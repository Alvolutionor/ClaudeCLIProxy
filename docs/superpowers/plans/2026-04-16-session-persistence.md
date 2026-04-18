# Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 proxy 记住每个对话的 Claude session_id，后续请求用 `--resume` 续接，Claude 自己维护上下文而不必每次传全量 history。

**Architecture:** 新增 `ConversationStore`（内存字典，映射 client session_id → Claude session_id）。`ClaudeCLI` 持有该 store，在每次调用后存入 Claude 返回的 session_id，下次调用时带上 `--resume`。服务器把 `x-session-id` header 透传给 CLI。当 `--resume` 失败（session 过期），自动降级为全量 prompt 重新开始。

**Tech Stack:** Python asyncio, aiohttp, pytest

---

## File Map

| 文件 | 操作 | 职责 |
|---|---|---|
| `claude_cli_proxy/conversation_store.py` | 新建 | client_id → claude_session_id 映射 |
| `claude_cli_proxy/cli.py` | 修改 | 捕获 session_id，传 `--resume`，降级逻辑 |
| `claude_cli_proxy/openai_compat.py` | 修改 | 新增 `last_user_prompt()` 提取最后一条用户消息 |
| `claude_cli_proxy/server.py` | 修改 | 将 `x-session-id` header 传给 CLI |
| `tests/test_conversation_store.py` | 新建 | ConversationStore 单元测试 |

---

### Task 1: ConversationStore

**Files:**
- Create: `claude_cli_proxy/conversation_store.py`
- Create: `tests/test_conversation_store.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_conversation_store.py
from claude_cli_proxy.conversation_store import ConversationStore


def test_set_and_get():
    store = ConversationStore()
    store.set("client-1", "claude-abc")
    assert store.get("client-1") == "claude-abc"


def test_get_missing_returns_none():
    store = ConversationStore()
    assert store.get("nonexistent") is None


def test_delete():
    store = ConversationStore()
    store.set("client-1", "claude-abc")
    store.delete("client-1")
    assert store.get("client-1") is None


def test_overwrite():
    store = ConversationStore()
    store.set("client-1", "claude-abc")
    store.set("client-1", "claude-xyz")
    assert store.get("client-1") == "claude-xyz"
```

- [ ] **Step 2: 跑测试确认失败**

```
pytest tests/test_conversation_store.py -v
```
Expected: `ModuleNotFoundError: No module named 'claude_cli_proxy.conversation_store'`

- [ ] **Step 3: 实现 ConversationStore**

```python
# claude_cli_proxy/conversation_store.py
"""对话 session 持久化存储。

映射 client 发来的 x-session-id → Claude CLI 返回的 session_id，
使后续请求可以用 --resume 续接，无需传全量 history。
"""


class ConversationStore:
    """内存映射：client_session_id → claude_session_id。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, client_id: str) -> str | None:
        return self._store.get(client_id)

    def set(self, client_id: str, claude_session_id: str) -> None:
        self._store[client_id] = claude_session_id

    def delete(self, client_id: str) -> None:
        self._store.pop(client_id, None)
```

- [ ] **Step 4: 跑测试确认通过**

```
pytest tests/test_conversation_store.py -v
```
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```
git add claude_cli_proxy/conversation_store.py tests/test_conversation_store.py
git commit -m "feat: add ConversationStore for client→claude session mapping"
```

---

### Task 2: last_user_prompt()

用 `--resume` 时只传最后一条用户消息（不传全量 history，因为 Claude 自己有 session 记忆）。

**Files:**
- Modify: `claude_cli_proxy/openai_compat.py`
- Test inline in existing test file（如无则手动测）

- [ ] **Step 1: 在 `openai_compat.py` 里加函数**

在 `messages_to_prompt` 定义之后加：

```python
def last_user_prompt(messages: list[dict]) -> str:
    """从消息列表中提取最后一条 user 消息的文本。

    用于 --resume 模式：session 已有历史，只需传最新的用户输入。
    如果没有 user 消息，返回空字符串。
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _extract_text(msg.get("content", ""))
    return ""
```

- [ ] **Step 2: 手动验证**

在 Python REPL 里：
```python
from claude_cli_proxy.openai_compat import last_user_prompt
msgs = [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "hi"},
    {"role": "user", "content": "what time is it?"},
]
assert last_user_prompt(msgs) == "what time is it?"
assert last_user_prompt([]) == ""
print("OK")
```

- [ ] **Step 3: Commit**

```
git add claude_cli_proxy/openai_compat.py
git commit -m "feat: add last_user_prompt() for resume-mode prompt extraction"
```

---

### Task 3: cli.py — 捕获 session_id（非流式路径）

`call()` 当前用默认输出格式（纯文本），无法拿到 session_id。改为 `--output-format json` 解析结构化输出。

**Files:**
- Modify: `claude_cli_proxy/cli.py:80-176`

- [ ] **Step 1: 修改 `_exec_once` 返回 `(text, session_id)`**

替换 `_exec_once` 方法：

```python
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
```

- [ ] **Step 2: 修改 `call()` 接受 `conv_id`，使用/存储 session**

替换 `call()` 方法（在 `__init__.py` 中也需要导入 `ConversationStore`，但 ClaudeCLI 直接构造它）：

在 `ClaudeCLI.__init__` 里加：
```python
from .conversation_store import ConversationStore
# 在 __init__ 方法里加一行：
self._conv_store = ConversationStore()
```

替换 `call()` 方法：

```python
async def call(
    self,
    prompt: str,
    model: str = "",
    cwd: str | None = None,
    conv_id: str | None = None,
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
        raise last_error  # type: ignore[misc]

    # 有已有 session → 尝试 --resume
    if claude_session_id:
        from .openai_compat import last_user_prompt as _last
        try:
            text, new_sid = await _attempt(_last(prompt.splitlines() and [prompt] or []), claude_session_id)
            if conv_id and new_sid:
                self._conv_store.set(conv_id, new_sid)
            return text
        except CLIError as e:
            logger.warning("[session] --resume %s failed (%s), falling back to full prompt", claude_session_id, e)
            self._conv_store.delete(conv_id)  # type: ignore[arg-type]
            claude_session_id = None

    # 无 session 或降级 → 全量 prompt
    text, new_sid = await _attempt(prompt, None)
    if conv_id and new_sid:
        self._conv_store.set(conv_id, new_sid)
    return text
```

- [ ] **Step 3: 跑已有测试确认不破坏**

```
pytest tests/ -v
```
Expected: 所有已有测试 PASSED（conv 相关不影响现有 registry/mcp 测试）

- [ ] **Step 4: Commit**

```
git add claude_cli_proxy/cli.py claude_cli_proxy/conversation_store.py
git commit -m "feat: capture session_id in call() and resume on subsequent requests"
```

---

### Task 4: cli.py — stream_call() 捕获 session_id

`stream_call()` 已用 `--output-format stream-json`，最后一行 `{"type":"result","session_id":"..."}` 包含 session_id。

**Files:**
- Modify: `claude_cli_proxy/cli.py` — `stream_call()` 方法

- [ ] **Step 1: 修改 `stream_call()` 接受 `conv_id`，在结束时存储 session**

替换 `stream_call()` 签名和末尾处理：

```python
async def stream_call(
    self,
    prompt: str,
    model: str = "",
    cwd: str | None = None,
    mcp_config_path: str | None = None,
    conv_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Spawn claude -p and yield stdout lines as they arrive.

    conv_id: 用于在流结束后存储 Claude session_id，供下次 --resume 使用。
    注意：stream_call 不支持 --resume（MCP bridge 每次都是独立 invocation），
    但仍需捕获 session_id 供非流式路径使用。
    """
    model = model or self.config.default_model
    env = self._build_env()
    target_cwd = cwd if cwd is not None else self.config.neutral_cwd

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
```

- [ ] **Step 2: 跑已有测试**

```
pytest tests/ -v
```
Expected: 全部 PASSED

- [ ] **Step 3: Commit**

```
git add claude_cli_proxy/cli.py
git commit -m "feat: capture session_id in stream_call() for future resume"
```

---

### Task 5: server.py — 透传 conv_id 给 CLI

**Files:**
- Modify: `claude_cli_proxy/server.py:140-175`

- [ ] **Step 1: 在非流式路径传 conv_id**

在 `handle_chat_completions` 里，非流式路径的 `cli.call()` 调用加上 `conv_id`：

```python
# 找到这行（约第 146 行）：
result = await cli.call(prompt, model, cwd=effective_cwd)
# 改为：
result = await cli.call(prompt, model, cwd=effective_cwd, conv_id=session_id)
```

- [ ] **Step 2: 在 MCP 流式路径传 conv_id 给 stream_call**

在 `_generate_tool_sse` 函数签名加 `conv_id` 参数，并传给 `cli.stream_call`：

```python
# 找到函数定义（约第 264 行）：
async def _generate_tool_sse(cli, session, prompt, model, cwd, mcp_config_path):
# 改为：
async def _generate_tool_sse(cli, session, prompt, model, cwd, mcp_config_path, conv_id=None):
```

在函数内找到 `stream_call` 调用（约第 301 行）：
```python
stdout_iter = cli.stream_call(prompt, model, cwd=cwd, mcp_config_path=mcp_config_path)
# 改为：
stdout_iter = cli.stream_call(prompt, model, cwd=cwd, mcp_config_path=mcp_config_path, conv_id=conv_id)
```

在 `handle_chat_completions` 里找到 `_generate_tool_sse` 调用：
```python
async for sse_line in _generate_tool_sse(cli, session, prompt, model, effective_cwd, mcp_config_path):
# 改为：
async for sse_line in _generate_tool_sse(cli, session, prompt, model, effective_cwd, mcp_config_path, conv_id=session_id):
```

- [ ] **Step 3: 跑全部测试**

```
pytest tests/ -v
```
Expected: 全部 PASSED

- [ ] **Step 4: Commit**

```
git add claude_cli_proxy/server.py
git commit -m "feat: wire conv_id through server to CLI for session persistence"
```

---

### Task 6: call() 的 resume prompt 修复

Task 3 里 `call()` 的 resume 路径用了一个奇怪的 hack（`prompt.splitlines() and [prompt] or []`）。正确做法是从外部传入 last_user_prompt，或在 call() 内把 prompt 直接当 last message 用。

**Files:**
- Modify: `claude_cli_proxy/cli.py` — `call()` 的 resume 分支

- [ ] **Step 1: 修复 resume prompt 提取逻辑**

在 `call()` 的 resume 分支里，prompt 参数已经是 `messages_to_prompt(messages)` 拼好的全量文本。resume 时需要的是最后一条用户消息。

server.py 需要同时传 `full_prompt` 和 `last_prompt`，或者 `call()` 直接接受 `messages`。最简方案：在 server.py 里多传一个 `resume_prompt` 参数。

修改 `call()` 签名：
```python
async def call(
    self,
    prompt: str,
    model: str = "",
    cwd: str | None = None,
    conv_id: str | None = None,
    resume_prompt: str | None = None,
) -> str:
```

修改 resume 分支：
```python
if claude_session_id:
    rp = resume_prompt if resume_prompt is not None else prompt
    try:
        text, new_sid = await _attempt(rp, claude_session_id)
        if conv_id and new_sid:
            self._conv_store.set(conv_id, new_sid)
        return text
    except CLIError as e:
        logger.warning("[session] --resume %s failed (%s), falling back", claude_session_id, e)
        self._conv_store.delete(conv_id)  # type: ignore[arg-type]
```

在 server.py 的 `cli.call()` 调用改为：
```python
from .openai_compat import last_user_prompt, messages_to_prompt
prompt = messages_to_prompt(messages)
resume_prompt = last_user_prompt(messages)
result = await cli.call(prompt, model, cwd=effective_cwd, conv_id=session_id, resume_prompt=resume_prompt)
```

- [ ] **Step 2: 跑全部测试**

```
pytest tests/ -v
```
Expected: 全部 PASSED

- [ ] **Step 3: Commit**

```
git add claude_cli_proxy/cli.py claude_cli_proxy/server.py
git commit -m "fix: pass correct last-user-message as resume prompt in call()"
```

---

## Self-Review

**Spec coverage:**
- ✅ ConversationStore：client_id → claude_session_id 映射
- ✅ call() 捕获 session_id（--output-format json）
- ✅ stream_call() 捕获 session_id（result event）
- ✅ --resume 用于后续请求
- ✅ session 过期降级处理
- ✅ resume 时只传 last_user_prompt
- ✅ x-session-id header 透传

**已知限制（YAGNI，不在本 plan 范围）：**
- session store 重启后清空（in-memory，够用）
- stream_call() 不支持 --resume（MCP bridge 每次独立调用，可后续加）
