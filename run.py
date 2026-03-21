#!/usr/bin/env python3
"""Entry point for Claude CLI Proxy server.

Usage:
  python run.py                                    # default port 8766
  python run.py --port 9000                        # custom port
  python run.py --model claude-sonnet-4-6          # default model
  python run.py --max-concurrent 5 --timeout 600   # tuning
"""

from claude_cli_proxy.config import ProxyConfig
from claude_cli_proxy.server import run_server


def main():
    config = ProxyConfig.from_cli_args()
    run_server(config)


if __name__ == "__main__":
    main()
