"""Configuration management for Claude CLI Proxy."""

import argparse
import os
from dataclasses import dataclass, field


@dataclass
class ProxyConfig:
    """All proxy server configuration in one place."""

    host: str = "0.0.0.0"
    port: int = 8766
    default_model: str = "claude-haiku-4-5-20251001"
    max_concurrent: int = 3
    git_bash_path: str = ""
    cli_timeout: int = 300

    def __post_init__(self):
        if not self.git_bash_path:
            self.git_bash_path = os.environ.get(
                "CLAUDE_CODE_GIT_BASH_PATH",
                "D:\\produce\\Git\\bin\\bash.exe",
            )

    @classmethod
    def from_cli_args(cls, args: list[str] | None = None) -> "ProxyConfig":
        """Parse CLI arguments into a ProxyConfig."""
        parser = argparse.ArgumentParser(
            description="Claude CLI → OpenAI-compatible API proxy"
        )
        parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
        parser.add_argument("--port", type=int, default=8766, help="Server port (default: 8766)")
        parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="Default model")
        parser.add_argument("--max-concurrent", type=int, default=3, help="Max concurrent CLI calls")
        parser.add_argument("--timeout", type=int, default=300, help="CLI call timeout in seconds")
        parser.add_argument("--git-bash-path", default="", help="Path to git bash executable")
        parsed = parser.parse_args(args)

        return cls(
            host=parsed.host,
            port=parsed.port,
            default_model=parsed.model,
            max_concurrent=parsed.max_concurrent,
            cli_timeout=parsed.timeout,
            git_bash_path=parsed.git_bash_path or "",
        )
