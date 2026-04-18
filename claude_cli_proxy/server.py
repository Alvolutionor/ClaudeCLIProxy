"""HTTP 服务器模块，提供 OpenAI 兼容的路由接口。

支持 CORS 跨域请求、SSE 流式传输和结构化错误响应。
"""

import asyncio
import json
import logging
import os
import socket
import time
import uuid

from aiohttp import web

from .cli import CLIError, ClaudeCLI
from .config import ProxyConfig
from .openai_compat import (
    build_chat_response,
    build_models_response,
    build_stream_chunks,
    last_user_prompt,
    messages_to_prompt,
)

logger = logging.getLogger("claude_cli_proxy.server")

# CORS 响应头 — 允许所有来源（代理服务运行在本地/局域网）
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """CORS 中间件：为所有响应添加跨域头，并处理 OPTIONS 预检请求。"""
    if request.method == "OPTIONS":
        return web.Response(status=200, headers=_CORS_HEADERS)
    response = await handler(request)
    response.headers.update(_CORS_HEADERS)
    return response


def create_app(config: ProxyConfig) -> web.Application:
    """创建并配置 aiohttp 应用实例。"""
    from .session_registry import SessionRegistry, ContextCache
    from .mcp_server import MCPServer

    app = web.Application(middlewares=[cors_middleware])
    app["config"] = config
    app["cli"] = ClaudeCLI(config)
    app["start_time"] = time.time()
    app["request_count"] = 0

    session_registry = SessionRegistry()
    mcp_server = MCPServer(
        session_registry,
        mcp_port=config.mcp_server_port,
        tool_timeout=config.tool_call_timeout,
    )
    app["session_registry"] = session_registry
    app["mcp_server"] = mcp_server
    app["context_cache"] = ContextCache()

    # Standard routes
    app.router.add_route("OPTIONS", "/v1/chat/completions", handle_options)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)

    # Tool bridge: phone POSTs tool results here
    app.router.add_post("/v1/sandbox/results/{call_id}", handle_tool_result)

    # MCP SSE transport routes (used by claude CLI subprocess)
    mcp_server.register_routes(app)

    return app


async def handle_options(request: web.Request) -> web.Response:
    """处理 OPTIONS 预检请求。"""
    return web.Response(status=200, headers=_CORS_HEADERS)


async def handle_tool_result(request: web.Request) -> web.Response:
    """POST /v1/sandbox/results/{call_id} — phone submits tool execution result."""
    call_id = request.match_info["call_id"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("Invalid JSON", "invalid_request_error", 400)

    from .session_registry import SessionRegistry
    registry: SessionRegistry = request.app["session_registry"]

    error = body.get("error")
    if error is not None:
        registry.reject_call(call_id, str(error))
    else:
        result = body.get("result", "")
        registry.resolve_call(call_id, str(result))

    return web.Response(status=204)


async def handle_chat_completions(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — chat completion with optional MCP tool bridge."""
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _error_response("Invalid JSON format", "invalid_request_error", 400)

    messages = body.get("messages", [])
    if not messages:
        return _error_response("messages field cannot be empty", "invalid_request_error", 400)

    config: ProxyConfig = request.app["config"]
    cli: ClaudeCLI = request.app["cli"]

    model = body.get("model", config.default_model)
    stream = body.get("stream", False)
    tools: list[dict] = body.get("tools", [])
    session_id = request.headers.get("x-session-id") or str(uuid.uuid4())
    conv_id = request.headers.get("x-session-id")

    # --- Session metadata (long-lived context) ---
    import json as _json
    raw_meta = request.headers.get("x-session-meta", "{}")
    try:
        session_meta: dict = _json.loads(raw_meta)
    except Exception:
        session_meta = {}

    # --- Request-level context (refreshed every request) ---
    open_file: dict | None = body.get("open_file")
    diagnostics: list[dict] = body.get("diagnostics", [])
    extra_prefetch: list[dict] = body.get("prefetch", [])
    refresh_context: bool = request.headers.get("x-refresh-context", "").lower() == "true"

    requested_cwd = body.get("cwd") or session_meta.get("cwd")
    cwd_mode = body.get("cwd_mode", "neutral")

    if requested_cwd is not None:
        if not isinstance(requested_cwd, str) or not requested_cwd.strip():
            return _error_response("cwd must be a non-empty string", "invalid_request_error", 400)
        effective_cwd = requested_cwd
    elif cwd_mode == "inherit":
        effective_cwd = None
    else:
        effective_cwd = config.neutral_cwd

    request.app["request_count"] += 1

    # No tools or non-streaming → fast path (no MCP overhead)
    if not tools or not stream:
        prompt = messages_to_prompt(messages)
        resume_prompt = last_user_prompt(messages)
        start = time.time()
        try:
            result = await cli.call(prompt, model, cwd=effective_cwd, conv_id=conv_id, resume_prompt=resume_prompt)
        except asyncio.TimeoutError:
            return _error_response(
                f"CLI call timed out ({config.cli_timeout}s)", "timeout_error", 504
            )
        except CLIError as e:
            return _error_response(str(e), "server_error", 500)
        except Exception as e:
            logger.exception("Unexpected error")
            return _error_response(str(e), "server_error", 500)
        elapsed = time.time() - start
        logger.info("[%s] %d chars, %.1fs (no tools)", model, len(prompt), elapsed)

        if stream:
            resp = web.StreamResponse(status=200, headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                **_CORS_HEADERS,
            })
            await resp.prepare(request)
            for chunk in build_stream_chunks(model, result):
                await resp.write(chunk.encode("utf-8"))
            await resp.write_eof()
            return resp

        return web.json_response(build_chat_response(model, result, prompt_chars=len(prompt)))

    # --- Tool bridge path (MCP + context pre-fetch) ---
    from .session_registry import SessionRegistry, ContextCache
    from .context_builder import run_prefetch, assemble_env_snapshot, build_cli_prompt

    registry: SessionRegistry = request.app["session_registry"]
    context_cache: ContextCache = request.app["context_cache"]

    if refresh_context:
        context_cache.invalidate(session_id)

    session = registry.register(session_id, tools)
    mcp_config_path = _write_mcp_config(session_id, config.mcp_server_port)

    logger.info("[%s] session=%s tools=%d (MCP bridge)", model, session_id, len(tools))

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        **_CORS_HEADERS,
    })
    await resp.prepare(request)

    async def _write(line: str) -> None:
        await resp.write(line.encode("utf-8"))

    try:
        # Pre-fetch phase: build env snapshot on first request for this session
        env_snapshot = context_cache.get(session_id)
        if env_snapshot is None:
            logger.info("[prefetch] session=%s building context snapshot", session_id)
            prefetch_results = await run_prefetch(
                session_id=session_id,
                registry=registry,
                tools=tools,
                extra_prefetch=extra_prefetch,
                tool_timeout=config.tool_call_timeout,
                write_sse=_write,
            )
            env_snapshot = assemble_env_snapshot(prefetch_results, session_meta)
            context_cache.set(session_id, env_snapshot)
            logger.info("[prefetch] session=%s snapshot ready (%d chars)", session_id, len(env_snapshot))

        prompt = build_cli_prompt(messages, env_snapshot, open_file, diagnostics)

        async for sse_line in _generate_tool_sse(cli, session, prompt, model, effective_cwd, mcp_config_path, conv_id=conv_id):
            await _write(sse_line)
    finally:
        registry.unregister(session_id)
        try:
            os.unlink(mcp_config_path)
        except OSError:
            pass
        await resp.write_eof()

    return resp


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models — 列出可用模型。"""
    return web.json_response(build_models_response())


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — 健康检查，返回服务器状态和运行时间。"""
    uptime = time.time() - request.app["start_time"]
    return web.json_response({
        "status": "ok",
        "provider": "claude-cli-proxy",
        "uptime_seconds": round(uptime, 1),
        "request_count": request.app["request_count"],
    })


def _error_response(message: str, error_type: str, status: int) -> web.Response:
    """构建统一的错误响应（兼容 OpenAI 错误格式）。

    参数:
        message: 错误描述文本。
        error_type: 错误类型标识符。
        status: HTTP 状态码。

    返回:
        包含错误信息的 JSON 响应。
    """
    return web.json_response(
        {"error": {"message": message, "type": error_type}},
        status=status,
        headers=_CORS_HEADERS,
    )


def _write_mcp_config(session_id: str, mcp_port: int) -> str:
    """Write a per-session MCP config file and return its path."""
    import tempfile
    config_data = {
        "mcpServers": {
            "mobile-sandbox": {
                "type": "sse",
                "url": f"http://127.0.0.1:{mcp_port}/mcp/{session_id}/sse",
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix=f"mcp-{session_id[:8]}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config_data, f)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


async def _generate_tool_sse(cli, session, prompt, model, cwd, mcp_config_path, conv_id=None):
    """Dual-source SSE generator: interleaves Claude text output with phone tool_call events.

    Yields SSE-formatted strings (already prefixed with "data: ").

    Sources:
      1. Claude CLI stdout (via cli.stream_call) — text content in stream-json format
      2. session.event_queue — PendingToolCall objects to forward to phone
    """
    from .cli import extract_text_from_stream_json, CLIError

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _text_chunk(text: str) -> str:
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def _tool_call_event(call) -> str:
        event = {
            "type": "tool_call",
            "id": call.id,
            "name": call.name,
            "input": call.tool_input,
        }
        return f"data: {json.dumps(event)}\n\n"

    # Opening role chunk
    yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"

    stdout_done = False
    stdout_iter = cli.stream_call(prompt, model, cwd=cwd, mcp_config_path=mcp_config_path, conv_id=conv_id)
    stdout_task: asyncio.Task | None = None
    queue_task: asyncio.Task | None = None
    _stdout_errors: list[Exception] = []  # mutable container so closure can write to it

    async def _next_stdout():
        try:
            return await stdout_iter.__anext__()
        except StopAsyncIteration:
            return None
        except Exception as exc:
            # CLIError or other subprocess error — capture and return None
            _stdout_errors.append(exc)
            return None

    stdout_task = asyncio.ensure_future(_next_stdout())
    queue_task = asyncio.ensure_future(session.event_queue.get())

    try:
        while stdout_task is not None or queue_task is not None:
            active = [t for t in [stdout_task, queue_task] if t is not None]
            if not active:
                break

            done_set, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)

            for task in done_set:
                if task is stdout_task:
                    line = task.result()
                    if line is None:
                        stdout_done = True
                        stdout_task = None
                        exc = _stdout_errors[0] if _stdout_errors else None
                        if exc is not None:
                            logger.error("[SSE] Claude CLI error: %s", exc)
                            error_chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": f"\n[Error: {exc}]"}, "finish_reason": "stop"}],
                            }
                            yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
                        # Stdout finished: push a sentinel so the queue consumer stops cleanly
                        # after draining any tool calls that were already enqueued.
                        await session.event_queue.put(None)
                    else:
                        text = extract_text_from_stream_json(line)
                        if text:
                            yield _text_chunk(text)
                        stdout_task = asyncio.ensure_future(_next_stdout())

                elif task is queue_task:
                    call = task.result()
                    if call is None:  # sentinel: stdout done, no more tool calls
                        queue_task = None
                    else:
                        yield _tool_call_event(call)
                        queue_task = asyncio.ensure_future(session.event_queue.get())

    finally:
        for t in [stdout_task, queue_task]:
            if t and not t.done():
                t.cancel()

    # Drain any remaining queued tool_calls (safety net; sentinel should have been consumed)
    while not session.event_queue.empty():
        call = session.event_queue.get_nowait()
        if call is not None:  # skip sentinel if it was not consumed by the loop
            yield _tool_call_event(call)

    # Stop chunk
    stop = {
        "id": chat_id, "object": "chat.completion.chunk",
        "created": created, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop)}\n\n"
    yield "data: [DONE]\n\n"


def run_server(config: ProxyConfig):
    """启动代理服务器。"""
    # 配置日志格式
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app = create_app(config)

    # 获取本机网络信息
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)

    # 打印启动信息
    print("=" * 50)
    print("  Claude CLI Proxy Server")
    print("=" * 50)
    print(f"  本地地址:   http://localhost:{config.port}/v1")
    print(f"  网络地址:   http://{local_ip}:{config.port}/v1")
    print(f"  默认模型:   {config.default_model}")
    print(f"  最大并发:   {config.max_concurrent}")
    print(f"  超时时间:   {config.cli_timeout}秒")
    print(f"  重试次数:   {config.retry_count}")
    if config.git_bash_path:
        print(f"  Git Bash:   {config.git_bash_path}")
    print(f"  CLI CWD:    {config.neutral_cwd} (default neutral)")
    print("-" * 50)
    print(f"  连接方式:")
    print(f'    base_url = "http://{local_ip}:{config.port}/v1"')
    print(f'    api_key  = "not-needed"')
    print(f"  支持: 流式传输 (stream=True) | CORS 跨域")
    print("=" * 50)
    print()

    web.run_app(app, host=config.host, port=config.port, print=None)
