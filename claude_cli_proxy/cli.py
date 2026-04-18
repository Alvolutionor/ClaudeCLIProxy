"""Claude CLI 子进程封装模块。

提供带有并发控制、自动重试和详细错误分类的 CLI 调用接口。
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator

from .config import ProxyConfig
from .conversation_store import ConversationStore

logger = logging.getLogger("claude_cli_proxy.cli")

# 禁止 Claude CLI 使用任何内置文件系统/执行工具。
# MCP 工具（mcp__*）不在此列表中，proxy 的 tool bridge 正常工作。
# --allowedTools "" 经测试无效（空字符串被忽略），必须用 --disallowedTools 黑名单。
_DISALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,MultiEdit,"
    "Glob,Grep,LS,"
    "WebFetch,WebSearch,"
    "NotebookRead,NotebookEdit,"
    "Task,Agent"
)


def extract_text_from_stream_json(line: str) -> str | None:
    """Parse one line of claude --output-format stream-json and return text if present.

    Claude CLI stream-json emits newline-delimited JSON. We handle the two
    shapes that carry visible text:

    {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
    {"type":"result","result":"...","session_id":"..."}

    Returns None for structural events (message_start, tool_use, etc.).
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        logger.debug("stream_call: non-JSON line (skipped): %.120s", line)
        return None

    t = obj.get("type", "")

    if t == "result":
        return obj.get("result") or None

    if t == "assistant":
        msg = obj.get("message", {})
        content = msg.get("content", [])
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(texts)
        return text if text else None

    return None  # Structural event — caller skips


class CLIError(RuntimeError):
    """CLI 调用失败时抛出的异常，包含返回码和原始错误输出。"""

    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode  # 进程退出码
        self.stderr = stderr          # 标准错误输出内容


class ClaudeCLI:
    """管理 Claude CLI 子进程调用，支持并发控制和自动重试。"""

    def __init__(self, config: ProxyConfig):
        self.config = config
        # 使用信号量限制最大并发调用数
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._conv_store = ConversationStore()

    def _build_env(self) -> dict[str, str]:
        """构建子进程的环境变量。

        硬隔离策略（经实测有效）：
        - CLAUDE_CODE_DISABLE_CLAUDE_MDS=1  禁止加载任何 CLAUDE.md（全局 + 项目级）
        - CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 禁止加载 ~/.claude/projects/ 记忆文件
        - CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1 禁止注入 git 上下文
        - CLAUDECODE 移除，避免子进程拒绝在 Claude Code session 内运行
        """
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        # ── 上下文硬隔离 ──────────────────────────────────────────────────────
        # CLAUDE_CODE_DISABLE_CLAUDE_MDS=1
        #   官方 env 变量，直接禁止 Claude CLI 加载任何 CLAUDE.md 文件
        #   （全局 ~/.claude/CLAUDE.md 和项目级 CLAUDE.md 全部跳过）。
        #   经测试有效：用户的 Work History / 全局规则不再注入。
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
        # CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
        #   禁止 Claude CLI 自动加载 ~/.claude/projects/ 下的记忆文件。
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        # CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1
        #   禁止将 git 状态/分支信息注入 Claude 上下文。
        env["CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"] = "1"

        if self.config.git_bash_path:
            env["CLAUDE_CODE_GIT_BASH_PATH"] = self.config.git_bash_path
        return env

    async def _exec_once(
        self, prompt_bytes: bytes, model_args: list[str], env: dict, cwd: str | None
    ) -> tuple[str, str | None]:
        """执行单次 CLI 调用，返回 (响应文本, claude_session_id)。

        使用 --output-format json 以便解析 session_id。
        """
        async with self._semaphore:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p",
                "--output-format", "json",
                "--disallowedTools", _DISALLOWED_TOOLS,
                *model_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt_bytes),
                timeout=self.config.cli_timeout,
            )

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            raise CLIError(
                f"Claude CLI error (return code {proc.returncode}): {err_text}",
                returncode=proc.returncode,
                stderr=err_text,
            )

        raw = stdout.decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(raw)
            text = obj.get("result", "") or ""
            session_id = obj.get("session_id") or None
        except (json.JSONDecodeError, ValueError):
            # 旧版 Claude CLI 可能输出纯文本，降级处理
            text = raw
            session_id = None
        return text, session_id

    async def call(
        self,
        prompt: str,
        model: str = "",
        cwd: str | None = None,
        conv_id: str | None = None,
        resume_prompt: str | None = None,
    ) -> str:
        """调用 Claude CLI，支持自动重试和 session 续接。

        conv_id: 来自请求的 x-session-id。如果该对话已有 Claude session，
                 自动用 --resume 续接（只传最后一条用户消息）。
                 如果 --resume 失败（session 过期），降级为全量 prompt 重新开始。
        """
        model = model or self.config.default_model
        env = self._build_env()
        model_args = ["--model", model] if model else []
        target_cwd = cwd if cwd is not None else self.config.neutral_cwd

        claude_session_id = self._conv_store.get(conv_id) if conv_id else None

        async def _attempt(prompt_text: str, resume_id: str | None) -> tuple[str, str | None]:
            args = list(model_args)
            if resume_id:
                args = ["--resume", resume_id] + args
            prompt_bytes = prompt_text.encode("utf-8")
            last_error: Exception | None = None
            for attempt in range(1 + self.config.retry_count):
                try:
                    return await self._exec_once(prompt_bytes, args, env, target_cwd)
                except asyncio.TimeoutError:
                    last_error = asyncio.TimeoutError(
                        f"CLI call timed out (waited {self.config.cli_timeout}s)"
                    )
                    logger.warning("CLI call timed out, attempt %d/%d", attempt + 1, 1 + self.config.retry_count)
                except CLIError as e:
                    last_error = e
                    logger.warning("CLI call failed (rc %d), attempt %d/%d: %s",
                                   e.returncode, attempt + 1, 1 + self.config.retry_count, e.stderr)
                except OSError as e:
                    raise CLIError(f"Failed to start Claude CLI: {e}") from e
                if attempt < self.config.retry_count:
                    await asyncio.sleep(self.config.retry_delay * (attempt + 1))
            if last_error is None:
                raise RuntimeError("_attempt: retry loop exited without error (retry_count misconfigured)")
            raise last_error

        # 有已有 session → 尝试 --resume
        if claude_session_id:
            rp = resume_prompt if resume_prompt is not None else prompt
            try:
                text, new_sid = await _attempt(rp, claude_session_id)
                if conv_id and new_sid:
                    self._conv_store.set(conv_id, new_sid)
                return text
            except CLIError as e:
                # asyncio.TimeoutError intentionally not caught here — timeout on --resume
                # propagates to the caller rather than triggering a full-prompt fallback.
                logger.warning("[session] --resume %s failed (%s), falling back to full prompt", claude_session_id, e)
                self._conv_store.delete(conv_id)  # type: ignore[arg-type]
                claude_session_id = None

        # 无 session 或降级 → 全量 prompt
        text, new_sid = await _attempt(prompt, None)
        if conv_id and new_sid:
            self._conv_store.set(conv_id, new_sid)
        return text

    async def stream_call(
        self,
        prompt: str,
        model: str = "",
        cwd: str | None = None,
        mcp_config_path: str | None = None,
        conv_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Spawn claude -p and yield stdout lines as they arrive.

        Used by the MCP tool bridge where Claude calls phone tools mid-generation.
        The caller is responsible for handling MCP tool calls concurrently via MCPServer.

        Session persistence (--resume) is intentionally not supported here; this method
        is used exclusively for the MCP tool-bridge path where each invocation is stateless.

        conv_id: if provided, the Claude session_id from the result event is stored in
                 conv_store for future --resume use.

        Yields:
            str: Each decoded line from Claude's stdout (newline stripped).

        Raises:
            CLIError: If Claude exits with non-zero status.
        """
        model = model or self.config.default_model
        env = self._build_env()
        target_cwd = cwd if cwd is not None else self.config.neutral_cwd

        # 始终禁止内置文件/执行工具；MCP 模式下 mcp__* 工具不受影响（不在黑名单里）。
        args = ["claude", "-p", "--output-format", "stream-json", "--disallowedTools", _DISALLOWED_TOOLS]
        if model:
            args += ["--model", model]
        if mcp_config_path:
            args += ["--mcp-config", mcp_config_path]

        async with self._semaphore:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=target_cwd,
            )
            prompt_bytes = prompt.encode("utf-8")
            proc.stdin.write(prompt_bytes)
            await proc.stdin.drain()
            proc.stdin.close()
        # Semaphore released here — process is spawned and stdin closed.
        # We yield lines outside the semaphore so long-running tool-bridge sessions
        # don't block the concurrency slot for their entire duration.

        try:
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                # 从 result 事件捕获 session_id
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "result" and conv_id:
                        sid = obj.get("session_id")
                        if sid:
                            self._conv_store.set(conv_id, sid)
                except (json.JSONDecodeError, ValueError):
                    pass
                yield line
        finally:
            await proc.wait()
            if proc.returncode != 0:
                stderr_bytes = await proc.stderr.read()
                err = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise CLIError(
                    f"Claude CLI error (exit {proc.returncode}): {err}",
                    returncode=proc.returncode,
                    stderr=err,
                )
