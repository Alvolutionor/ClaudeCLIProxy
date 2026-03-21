"""HTTP server with OpenAI-compatible routes."""

import json
import socket
import time

from aiohttp import web

from .cli import ClaudeCLI
from .config import ProxyConfig
from .openai_compat import build_chat_response, build_models_response, messages_to_prompt


def create_app(config: ProxyConfig) -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application()
    app["config"] = config
    app["cli"] = ClaudeCLI(config)

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)

    return app


async def handle_chat_completions(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — OpenAI-compatible chat endpoint."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    messages = body.get("messages", [])
    if not messages:
        return web.json_response(
            {"error": {"message": "messages is required", "type": "invalid_request_error"}},
            status=400,
        )

    config: ProxyConfig = request.app["config"]
    cli: ClaudeCLI = request.app["cli"]

    model = body.get("model", config.default_model)
    prompt = messages_to_prompt(messages)

    start = time.time()
    try:
        result = await cli.call(prompt, model)
    except Exception as e:
        return web.json_response(
            {"error": {"message": str(e), "type": "server_error"}},
            status=500,
        )
    elapsed = time.time() - start

    print(f"  [{model}] {len(prompt)}→{len(result)} chars, {elapsed:.1f}s")
    response = build_chat_response(model, result)
    return web.json_response(response)


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models — list available models."""
    return web.json_response(build_models_response())


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — health check."""
    return web.json_response({"status": "ok", "provider": "claude-cli-proxy"})


def run_server(config: ProxyConfig):
    """Start the proxy server."""
    app = create_app(config)

    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)

    print("Claude CLI Proxy Server")
    print(f"  Local:   http://localhost:{config.port}/v1")
    print(f"  LAN:     http://{local_ip}:{config.port}/v1")
    print(f"  Model:   {config.default_model}")
    print(f"  Max concurrent: {config.max_concurrent}")
    print(f"\nConnect with:")
    print(f'  base_url = "http://{local_ip}:{config.port}/v1"')
    print(f'  api_key  = "not-needed"')
    print()

    web.run_app(app, host=config.host, port=config.port, print=None)
