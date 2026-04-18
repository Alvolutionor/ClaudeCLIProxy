"""Claude CLI Proxy — 封装 Claude CLI 的 OpenAI 兼容 API 服务器。

功能特性:
- 兼容 OpenAI 的 /v1/chat/completions 和 /v1/models 接口
- 支持 SSE 流式响应 (stream=True)
- 支持 CORS 跨域请求
- 自动重试与超时处理
- 并发控制
"""

__version__ = "0.2.0"

# 导出核心组件：CLI 封装、配置管理、服务器创建与启动
from .cli import CLIError, ClaudeCLI
from .config import ProxyConfig
from .server import create_app, run_server

__all__ = ["CLIError", "ClaudeCLI", "ProxyConfig", "create_app", "run_server"]
