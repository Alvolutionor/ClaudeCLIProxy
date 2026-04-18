#!/usr/bin/env python3
"""Claude CLI 代理服务器的入口文件。

用法:
  python run.py                                    # 默认端口 8766
  python run.py --port 9000                        # 自定义端口
  python run.py --model claude-sonnet-4-6          # 设置默认模型
  python run.py --max-concurrent 5 --timeout 600   # 调整并发数和超时时间
  python run.py --retry-count 3 --retry-delay 2.0  # 自定义重试策略
"""

from claude_cli_proxy.config import ProxyConfig
from claude_cli_proxy.server import run_server


def main():
    """从命令行参数加载配置并启动服务器。"""
    config = ProxyConfig.from_cli_args()
    run_server(config)


if __name__ == "__main__":
    main()
