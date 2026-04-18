"""Claude CLI 代理服务器的配置管理模块。"""

import argparse
import os
import shutil
import tempfile
from dataclasses import dataclass


def _detect_git_bash() -> str:
    """自动检测 Git Bash 可执行文件路径。

    搜索优先级:
    1. CLAUDE_CODE_GIT_BASH_PATH 环境变量
    2. 系统 PATH 中的 bash
    3. Windows 常见安装路径
    """
    # 优先检查环境变量
    env_path = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "")
    if env_path and os.path.isfile(env_path):
        return env_path
    # 尝试从系统 PATH 中查找 bash
    found = shutil.which("bash")
    if found:
        return found
    # 回退到 Windows 常见安装路径
    for candidate in [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return ""


@dataclass
class ProxyConfig:
    """代理服务器的所有配置选项。"""

    host: str = "0.0.0.0"                            # 绑定地址
    port: int = 8766                                  # 服务器端口
    default_model: str = "claude-haiku-4-5-20251001"  # 默认模型
    max_concurrent: int = 3                           # 最大并发 CLI 调用数
    cli_timeout: int = 300                            # 超时时间（秒）
    git_bash_path: str = ""                           # Git Bash 路径（空值 = 自动检测）
    retry_count: int = 2                              # 失败重试次数
    retry_delay: float = 1.0                          # 重试间隔时间（秒）
    neutral_cwd: str = ""                             # 默认中立工作目录（避免注入错误 repo 上下文）
    mcp_server_port: int = 18766           # Internal MCP server port for tool bridge
    tool_call_timeout: int = 60            # Seconds to wait for phone to return tool result

    def __post_init__(self):
        # 如果未指定 Git Bash 路径，则自动检测
        if not self.git_bash_path:
            self.git_bash_path = _detect_git_bash()
        if not self.neutral_cwd:
            self.neutral_cwd = os.path.join(tempfile.gettempdir(), "claude-cli-proxy-neutral")
        os.makedirs(self.neutral_cwd, exist_ok=True)

    @classmethod
    def from_cli_args(cls, args: list[str] | None = None) -> "ProxyConfig":
        """从命令行参数创建配置对象。"""
        parser = argparse.ArgumentParser(
            description="Claude CLI → OpenAI 兼容 API 代理"
        )
        parser.add_argument("--host", default="0.0.0.0", help="绑定地址（默认: 0.0.0.0）")
        parser.add_argument("--port", type=int, default=8766, help="服务器端口（默认: 8766）")
        parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="默认模型")
        parser.add_argument("--max-concurrent", type=int, default=3, help="最大并发 CLI 调用数")
        parser.add_argument("--timeout", type=int, default=300, help="CLI 调用超时时间（秒）")
        parser.add_argument("--git-bash-path", default="", help="Git Bash 路径（空值 = 自动检测）")
        parser.add_argument("--retry-count", type=int, default=2, help="失败重试次数（默认: 2）")
        parser.add_argument("--retry-delay", type=float, default=1.0, help="重试间隔时间（秒，默认: 1.0）")
        parser.add_argument("--neutral-cwd", default="", help="默认中立工作目录（空值 = 自动创建 temp 目录）")
        parser.add_argument("--mcp-server-port", type=int, default=18766, help="Internal MCP server port (default: 18766)")
        parser.add_argument("--tool-call-timeout", type=int, default=60, help="Tool call timeout in seconds (default: 60)")
        parsed = parser.parse_args(args)

        return cls(
            host=parsed.host,
            port=parsed.port,
            default_model=parsed.model,
            max_concurrent=parsed.max_concurrent,
            cli_timeout=parsed.timeout,
            git_bash_path=parsed.git_bash_path,
            retry_count=parsed.retry_count,
            retry_delay=parsed.retry_delay,
            neutral_cwd=parsed.neutral_cwd,
            mcp_server_port=parsed.mcp_server_port,
            tool_call_timeout=parsed.tool_call_timeout,
        )
