"""MCP SSE transport server for the mobile sandbox tool bridge.

Implements the MCP (Model Context Protocol) SSE transport so Claude CLI
can discover and call tools registered by the phone.

Transport spec:
  GET  /mcp/{session_id}/sse      — Claude CLI opens SSE connection; we stream JSON-RPC responses
  POST /mcp/{session_id}/messages — Claude CLI sends JSON-RPC requests here; we return 202 immediately

JSON-RPC methods handled:
  initialize              → server capabilities
  notifications/initialized → no-op
  tools/list              → list tools for this session
  tools/call              → forward to phone via session.event_queue, block until result
"""

import asyncio
import json
import logging
import secrets

from aiohttp import web

from .session_registry import SessionRegistry, SessionNotFoundError

logger = logging.getLogger("claude_cli_proxy.mcp_server")


def _log_task_exception(task: asyncio.Task) -> None:
    """Log any exception raised by a fire-and-forget asyncio task."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.error("[MCP] unhandled exception in background task", exc_info=exc)


_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


class MCPServer:
    def __init__(
        self,
        session_registry: SessionRegistry,
        mcp_port: int = 18766,
        tool_timeout: float = 60.0,
    ):
        self._registry = session_registry
        self._port = mcp_port
        self._tool_timeout = tool_timeout
        # Maps session_id -> asyncio.Queue of JSON-RPC response dicts
        # Queue is populated by _process_message, drained by handle_sse
        self._response_queues: dict[str, asyncio.Queue] = {}

    def register_routes(self, app: web.Application) -> None:
        """Add MCP routes to the aiohttp app."""
        app.router.add_get("/mcp/{session_id}/sse", self.handle_sse)
        app.router.add_post("/mcp/{session_id}/messages", self.handle_messages)
        app.router.add_route("OPTIONS", "/mcp/{session_id}/sse", _handle_options)
        app.router.add_route("OPTIONS", "/mcp/{session_id}/messages", _handle_options)

    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        """Claude CLI connects here to receive JSON-RPC responses."""
        session_id = request.match_info["session_id"]

        response_queue: asyncio.Queue = asyncio.Queue()
        self._response_queues[session_id] = response_queue

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                **_CORS,
            },
        )
        await resp.prepare(request)

        # MCP SSE transport: first send the endpoint URL for POST requests
        messages_url = f"http://127.0.0.1:{self._port}/mcp/{session_id}/messages"
        await resp.write(f"event: endpoint\ndata: {messages_url}\n\n".encode())

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(response_queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send keepalive ping
                    await resp.write(b": ping\n\n")
                    continue

                if msg is None:  # sentinel — session ended
                    break

                await resp.write(
                    f"event: message\ndata: {json.dumps(msg)}\n\n".encode()
                )
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._response_queues.pop(session_id, None)

        return resp

    async def handle_messages(self, request: web.Request) -> web.Response:
        """Claude CLI POSTs JSON-RPC requests here. Always return 202 immediately."""
        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        # Schedule processing without blocking the HTTP response
        task = asyncio.ensure_future(self._process_message(session_id, body))
        task.add_done_callback(_log_task_exception)
        return web.Response(status=202)

    async def _process_message(self, session_id: str, body: dict) -> None:
        """Process one JSON-RPC message and send response via SSE queue."""
        method = body.get("method", "")
        msg_id = body.get("id")  # None for notifications

        queue = self._response_queues.get(session_id)

        async def send(result=None, error=None):
            if queue is None or msg_id is None:
                return  # notification — no response needed
            resp: dict = {"jsonrpc": "2.0", "id": msg_id}
            if error is not None:
                resp["error"] = error
            else:
                resp["result"] = result
            await queue.put(resp)

        if method == "initialize":
            await send(result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mobile-sandbox", "version": "1.0.0"},
            })

        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass  # notifications have no id and need no response

        elif method == "tools/list":
            try:
                session = self._registry.get(session_id)
            except SessionNotFoundError:
                await send(result={"tools": []})
                return

            tools = []
            for t in session.tools:
                fn = t.get("function", {})
                tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "inputSchema": fn.get("parameters", {
                        "type": "object", "properties": {}
                    }),
                })
            await send(result={"tools": tools})

        elif method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            tool_input = params.get("arguments", {})
            call_id = f"tc_{secrets.token_hex(8)}"

            try:
                session = self._registry.get(session_id)
            except SessionNotFoundError:
                await send(error={"code": -32000, "message": "Session not found"})
                return

            call = self._registry.add_pending_call(
                session_id, call_id, tool_name, tool_input
            )

            # Notify SSE stream generator so it forwards the call to the phone
            await session.event_queue.put(call)

            logger.debug("[MCP] tool_call %s(%s) → waiting for phone", tool_name, call_id)

            try:
                await asyncio.wait_for(
                    call.result_event.wait(), timeout=self._tool_timeout
                )
            except asyncio.TimeoutError:
                logger.warning("[MCP] tool_call %s timed out after %.0fs", call_id, self._tool_timeout)
                await send(result={
                    "isError": True,
                    "content": [{"type": "text", "text": f"Tool call timed out after {self._tool_timeout:.0f}s"}],
                })
                return

            if call.error is not None:
                logger.debug("[MCP] tool_call %s returned error: %s", call_id, call.error)
                await send(result={
                    "isError": True,
                    "content": [{"type": "text", "text": call.error}],
                })
            else:
                logger.debug("[MCP] tool_call %s resolved OK", call_id)
                await send(result={
                    "content": [{"type": "text", "text": call.result or ""}],
                })

        else:
            logger.debug("[MCP] unknown method: %s", method)
            if msg_id is not None:
                await send(error={"code": -32601, "message": f"Method not found: {method}"})


async def _handle_options(request: web.Request) -> web.Response:
    return web.Response(status=200, headers=_CORS)
