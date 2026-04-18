# MCP Tool Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ClaudeCLIProxy a true mobile sandbox tool bridge — when mobileAgent sends a request with tool schemas, Claude CLI calls those tools via MCP, the proxy forwards calls to the phone, the phone executes them in the sandbox, and results flow back to Claude.

**Architecture:** Phone sends OpenAI-format request with `tools` array → Proxy registers session, writes per-session MCP config file, spawns `claude -p --mcp-config` → Claude calls tools via MCP JSON-RPC to our embedded MCP server → MCP server queues call into session's event_queue → Dual-source SSE generator interleaves text deltas (from Claude stdout) with `tool_call` events (from event_queue) → Phone executes tool, POSTs result to `/v1/sandbox/results/{call_id}` → MCP server resolves awaiting coroutine → Claude gets result and continues.

**Tech Stack:** Python 3.11+ / aiohttp (proxy), MCP SSE transport (JSON-RPC 2.0 over SSE), TypeScript / React Native Expo (mobileAgent)

**Spec:** `docs/superpowers/specs/2026-04-16-mcp-tool-bridge-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `claude_cli_proxy/session_registry.py` | CREATE | Per-request session state, pending tool call lifecycle |
| `claude_cli_proxy/mcp_server.py` | CREATE | MCP SSE transport, JSON-RPC handlers, tool call routing |
| `claude_cli_proxy/config.py` | MODIFY | Add `mcp_server_port`, `tool_call_timeout` |
| `claude_cli_proxy/cli.py` | MODIFY | Add `stream_call()` async-generator, `--mcp-config` arg |
| `claude_cli_proxy/server.py` | MODIFY | Accept `tools`, dual-source SSE, tool-result endpoint, MCP wiring |
| `tests/test_session_registry.py` | CREATE | Unit tests for SessionRegistry |
| `tests/test_mcp_server.py` | CREATE | Integration tests for MCP JSON-RPC handlers |
| `src/services/AIService.ts` *(mobileAgent)* | MODIFY | Send tools, handle `tool_call` SSE events, POST results, remove guardrail for sandbox mode |

---

## Task 1: SessionRegistry

**Files:**
- Create: `claude_cli_proxy/session_registry.py`
- Create: `tests/test_session_registry.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/test_session_registry.py`:

```python
import asyncio
import pytest
from claude_cli_proxy.session_registry import SessionRegistry, SessionNotFoundError


def test_register_and_get():
    reg = SessionRegistry()
    reg.register("s1", [{"function": {"name": "read_file"}}])
    session = reg.get("s1")
    assert session.session_id == "s1"
    assert len(session.tools) == 1


def test_get_unknown_raises():
    reg = SessionRegistry()
    with pytest.raises(SessionNotFoundError):
        reg.get("does-not-exist")


def test_unregister_cleans_up():
    reg = SessionRegistry()
    reg.register("s1", [])
    reg.unregister("s1")
    with pytest.raises(SessionNotFoundError):
        reg.get("s1")


@pytest.mark.asyncio
async def test_add_and_resolve_pending_call():
    reg = SessionRegistry()
    reg.register("s1", [])
    call = reg.add_pending_call("s1", "tc1", "read_file", {"path": "foo.ts"})
    assert call.id == "tc1"
    assert not call.result_event.is_set()

    reg.resolve_call("tc1", "file contents")
    assert call.result_event.is_set()
    assert call.result == "file contents"


@pytest.mark.asyncio
async def test_reject_pending_call():
    reg = SessionRegistry()
    reg.register("s1", [])
    call = reg.add_pending_call("s1", "tc2", "edit_file", {})
    reg.reject_call("tc2", "permission denied")
    assert call.result_event.is_set()
    assert call.error == "permission denied"


def test_resolve_unknown_call_is_noop():
    reg = SessionRegistry()
    # Should not raise; call might have already timed out
    reg.resolve_call("nonexistent-call-id", "result")
```

- [ ] **Step 1.2: Run tests to confirm they fail**

```bash
cd C:/Users/MSI_NB/Desktop/ClaudeCLIProxy
pip install pytest pytest-asyncio -q
pytest tests/test_session_registry.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'claude_cli_proxy.session_registry'`

- [ ] **Step 1.3: Implement SessionRegistry**

Create `claude_cli_proxy/session_registry.py`:

```python
"""Per-request session state for MCP tool bridge.

Each chat completion request that includes tools gets a SessionState.
Lifetime is bounded to the HTTP request; caller must call unregister() in a finally block.
"""

import asyncio
from dataclasses import dataclass, field


class SessionNotFoundError(KeyError):
    pass


@dataclass
class PendingToolCall:
    """A tool call dispatched to the phone and awaiting its result."""
    id: str                          # unique call id, e.g. "tc_a3f9b2"
    name: str                        # tool name, e.g. "read_file"
    input: dict                      # tool arguments
    result_event: asyncio.Event = field(default_factory=asyncio.Event)
    result: str | None = None        # resolved by resolve_call()
    error: str | None = None         # set by reject_call()


@dataclass
class SessionState:
    session_id: str
    tools: list[dict]                # OpenAI-format tool objects from phone
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    # Maps call_id -> PendingToolCall for in-flight tool calls
    _pending: dict[str, PendingToolCall] = field(default_factory=dict)


class SessionRegistry:
    """Thread-safe (asyncio-safe) registry of active sandbox sessions."""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        # Global call_id -> session_id reverse index
        self._call_index: dict[str, str] = {}

    def register(self, session_id: str, tools: list[dict]) -> SessionState:
        state = SessionState(session_id=session_id, tools=tools)
        self._sessions[session_id] = state
        return state

    def get(self, session_id: str) -> SessionState:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFoundError(session_id)

    def unregister(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            # Clean up call index entries for this session
            for call_id in list(session._pending):
                self._call_index.pop(call_id, None)

    def add_pending_call(
        self, session_id: str, call_id: str, name: str, input: dict
    ) -> PendingToolCall:
        session = self.get(session_id)
        call = PendingToolCall(id=call_id, name=name, input=input)
        session._pending[call_id] = call
        self._call_index[call_id] = session_id
        return call

    def resolve_call(self, call_id: str, result: str) -> None:
        """Called by POST /v1/sandbox/results/{call_id} when phone returns tool result."""
        session_id = self._call_index.get(call_id)
        if not session_id:
            return  # Already timed out or session gone — ignore
        session = self._sessions.get(session_id)
        if not session:
            return
        call = session._pending.get(call_id)
        if call:
            call.result = result
            call.result_event.set()

    def reject_call(self, call_id: str, error: str) -> None:
        """Called when phone reports a tool execution error."""
        session_id = self._call_index.get(call_id)
        if not session_id:
            return
        session = self._sessions.get(session_id)
        if not session:
            return
        call = session._pending.get(call_id)
        if call:
            call.error = error
            call.result_event.set()
```

- [ ] **Step 1.4: Run tests — expect PASS**

```bash
pytest tests/test_session_registry.py -v
```

Expected:
```
PASSED tests/test_session_registry.py::test_register_and_get
PASSED tests/test_session_registry.py::test_get_unknown_raises
PASSED tests/test_session_registry.py::test_unregister_cleans_up
PASSED tests/test_session_registry.py::test_add_and_resolve_pending_call
PASSED tests/test_session_registry.py::test_reject_pending_call
PASSED tests/test_session_registry.py::test_resolve_unknown_call_is_noop
6 passed
```

- [ ] **Step 1.5: Commit**

```bash
git add claude_cli_proxy/session_registry.py tests/test_session_registry.py
git commit -m "feat: add SessionRegistry for MCP tool bridge sessions"
```

---

## Task 2: MCP Server

**Files:**
- Create: `claude_cli_proxy/mcp_server.py`
- Create: `tests/test_mcp_server.py`

- [ ] **Step 2.1: Write the failing tests**

Create `tests/test_mcp_server.py`:

```python
"""Integration tests for MCP JSON-RPC handlers.

We instantiate MCPServer directly and call its internal _process_message()
to test each JSON-RPC method without needing a running HTTP server.
"""

import asyncio
import pytest
from claude_cli_proxy.session_registry import SessionRegistry
from claude_cli_proxy.mcp_server import MCPServer

SESSION_ID = "test-session"


@pytest.fixture
def registry_with_tools():
    reg = SessionRegistry()
    reg.register(SESSION_ID, [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                }
            }
        }
    ])
    return reg


@pytest.mark.asyncio
async def test_initialize_returns_capabilities(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    await server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}}
    })

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 1
    assert msg["result"]["capabilities"]["tools"] == {}
    assert msg["result"]["serverInfo"]["name"] == "mobile-sandbox"


@pytest.mark.asyncio
async def test_tools_list_returns_registered_tools(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    await server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    })

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    tools = msg["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "read_file"
    assert "path" in tools[0]["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_tools_call_enqueues_event_and_resolves(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    session = registry_with_tools.get(SESSION_ID)

    # Start tools/call in background (it will block until result)
    task = asyncio.ensure_future(server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "src/app.tsx"}}
    }))

    # The call should appear in the session's event_queue
    call = await asyncio.wait_for(session.event_queue.get(), timeout=1.0)
    assert call.name == "read_file"
    assert call.input == {"path": "src/app.tsx"}

    # Simulate phone returning result
    registry_with_tools.resolve_call(call.id, "import React from 'react';")

    await asyncio.wait_for(task, timeout=1.0)

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 3
    assert msg["result"]["content"][0]["text"] == "import React from 'react';"


@pytest.mark.asyncio
async def test_tools_call_timeout(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999, tool_timeout=0.1)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    await server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 4,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "x"}}
    })

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 4
    assert msg["result"]["isError"] is True
    assert "timed out" in msg["result"]["content"][0]["text"]
```

- [ ] **Step 2.2: Run tests to confirm they fail**

```bash
pytest tests/test_mcp_server.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'claude_cli_proxy.mcp_server'`

- [ ] **Step 2.3: Implement MCPServer**

Create `claude_cli_proxy/mcp_server.py`:

```python
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
        asyncio.ensure_future(self._process_message(session_id, body))
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
            if error:
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

            if call.error:
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
```

- [ ] **Step 2.4: Run tests — expect PASS**

```bash
pytest tests/test_mcp_server.py -v
```

Expected:
```
PASSED tests/test_mcp_server.py::test_initialize_returns_capabilities
PASSED tests/test_mcp_server.py::test_tools_list_returns_registered_tools
PASSED tests/test_mcp_server.py::test_tools_call_enqueues_event_and_resolves
PASSED tests/test_mcp_server.py::test_tools_call_timeout
4 passed
```

- [ ] **Step 2.5: Commit**

```bash
git add claude_cli_proxy/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: add MCPServer for MCP SSE transport (JSON-RPC tool bridge)"
```

---

## Task 3: Config additions + Streaming CLI

**Files:**
- Modify: `claude_cli_proxy/config.py`
- Modify: `claude_cli_proxy/cli.py`

- [ ] **Step 3.1: Add config fields**

Edit `claude_cli_proxy/config.py` — add two fields to the `ProxyConfig` dataclass after line 48 (`neutral_cwd`):

```python
    mcp_server_port: int = 18766           # Internal MCP server port for tool bridge
    tool_call_timeout: int = 60            # Seconds to wait for phone to return tool result
```

Also add `--mcp-server-port` and `--tool-call-timeout` to `from_cli_args()` — add these two lines after the `--neutral-cwd` argument (around line 72):

```python
        parser.add_argument("--mcp-server-port", type=int, default=18766, help="Internal MCP server port (default: 18766)")
        parser.add_argument("--tool-call-timeout", type=int, default=60, help="Tool call timeout in seconds (default: 60)")
```

And add them to the `return cls(...)` call (after `neutral_cwd=parsed.neutral_cwd`):

```python
            mcp_server_port=parsed.mcp_server_port,
            tool_call_timeout=parsed.tool_call_timeout,
```

- [ ] **Step 3.2: Add streaming CLI method**

Add `stream_call()` method to `ClaudeCLI` in `claude_cli_proxy/cli.py`. Insert after line 138 (end of `call()` method):

```python
    async def stream_call(
        self,
        prompt: str,
        model: str = "",
        cwd: str | None = None,
        mcp_config_path: str | None = None,
    ):
        """Spawn claude -p and yield stdout lines as they arrive.

        Used by the MCP tool bridge where Claude calls phone tools mid-generation.
        The caller is responsible for handling MCP tool calls concurrently via MCPServer.

        Yields:
            str: Each decoded line from Claude's stdout (newline stripped).

        Raises:
            CLIError: If Claude exits with non-zero status.
        """
        model = model or self.config.default_model
        env = self._build_env()
        target_cwd = cwd if cwd is not None else self.config.neutral_cwd

        args = ["claude", "-p", "--output-format", "stream-json"]
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
                    if line:
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

- [ ] **Step 3.3: Add helper to extract text from stream-json line**

Add this module-level function to `claude_cli_proxy/cli.py` (insert after the `logger` line, before the `CLIError` class):

```python
def extract_text_from_stream_json(line: str) -> str | None:
    """Parse one line of claude --output-format stream-json and return text if present.

    Claude CLI stream-json emits newline-delimited JSON.  We handle the two
    shapes that carry visible text:

    {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
    {"type":"result","result":"...","session_id":"..."}

    Returns None for structural events (message_start, tool_use, etc.).
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # Plain text line — return as-is
        return line

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
```

Also add `import json` at the top of `cli.py` (after the existing imports).

- [ ] **Step 3.4: Verify the module imports cleanly**

```bash
cd C:/Users/MSI_NB/Desktop/ClaudeCLIProxy
python -c "from claude_cli_proxy.cli import ClaudeCLI, extract_text_from_stream_json; print('OK')"
```

Expected: `OK`

- [ ] **Step 3.5: Commit**

```bash
git add claude_cli_proxy/config.py claude_cli_proxy/cli.py
git commit -m "feat: add stream_call() to ClaudeCLI and mcp_server_port config"
```

---

## Task 4: Server Routes — MCP wiring, dual-source SSE, tool-result endpoint

**Files:**
- Modify: `claude_cli_proxy/server.py`

- [ ] **Step 4.1: Rewrite `create_app()` to wire in MCP server and session registry**

Replace `create_app()` in `claude_cli_proxy/server.py` (lines 44–58) with:

```python
def create_app(config: ProxyConfig) -> web.Application:
    """Create and configure the aiohttp application."""
    from .session_registry import SessionRegistry
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
```

- [ ] **Step 4.2: Add `handle_tool_result` handler**

Add after the `handle_options` function in `server.py`:

```python
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
    if error:
        registry.reject_call(call_id, str(error))
    else:
        result = body.get("result", "")
        registry.resolve_call(call_id, str(result))

    return web.Response(status=204)
```

- [ ] **Step 4.3: Replace `handle_chat_completions` with tool-aware version**

Replace the entire `handle_chat_completions` function (lines 66–140) with:

```python
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

    requested_cwd = body.get("cwd")
    cwd_mode = body.get("cwd_mode", "neutral")

    if requested_cwd is not None:
        if not isinstance(requested_cwd, str) or not requested_cwd.strip():
            return _error_response("cwd must be a non-empty string", "invalid_request_error", 400)
        if not os.path.isdir(requested_cwd):
            return _error_response(f"cwd does not exist: {requested_cwd}", "invalid_request_error", 400)
        effective_cwd = requested_cwd
    elif cwd_mode == "inherit":
        effective_cwd = None
    else:
        effective_cwd = config.neutral_cwd

    request.app["request_count"] += 1

    # No tools → fast path (existing behaviour, no MCP overhead)
    if not tools or not stream:
        from .openai_compat import messages_to_prompt, build_stream_chunks, build_chat_response
        prompt = messages_to_prompt(messages)
        start = time.time()
        try:
            result = await cli.call(prompt, model, cwd=effective_cwd)
        except asyncio.TimeoutError:
            return _error_response(
                f"CLI call timed out ({config.cli_timeout}s)", "timeout_error", 504
            )
        except CLIError as e:
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

        from .openai_compat import build_chat_response
        return web.json_response(build_chat_response(model, result, prompt_chars=len(prompt)))

    # Tool bridge path — uses MCP
    from .session_registry import SessionRegistry
    from .openai_compat import messages_to_prompt
    registry: SessionRegistry = request.app["session_registry"]
    session = registry.register(session_id, tools)

    mcp_config_path = _write_mcp_config(session_id, config.mcp_server_port)
    prompt = messages_to_prompt(messages)

    logger.info("[%s] session=%s tools=%d (MCP bridge)", model, session_id, len(tools))

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        **_CORS_HEADERS,
    })
    await resp.prepare(request)

    try:
        async for sse_line in _generate_tool_sse(cli, registry, session, prompt, model, effective_cwd, mcp_config_path):
            await resp.write(sse_line.encode("utf-8"))
    finally:
        registry.unregister(session_id)
        try:
            os.unlink(mcp_config_path)
        except OSError:
            pass
        await resp.write_eof()

    return resp
```

- [ ] **Step 4.4: Add `_write_mcp_config` and `_generate_tool_sse` helpers**

Add these two functions after `handle_tool_result` in `server.py`:

```python
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
    fd, path = tempfile.mkstemp(prefix=f"mcp-{session_id}-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config_data, f)
    except Exception:
        os.close(fd)
        raise
    return path


async def _generate_tool_sse(cli, registry, session, prompt, model, cwd, mcp_config_path):
    """Dual-source SSE generator: interleaves Claude text output with phone tool_call events.

    Yields SSE-formatted strings (already prefixed with "data: ").

    Sources:
      1. Claude CLI stdout (via cli.stream_call) — text content
      2. session.event_queue — PendingToolCall objects forwarded to phone
    """
    from .cli import extract_text_from_stream_json, CLIError
    import uuid as _uuid

    chat_id = f"chatcmpl-{_uuid.uuid4().hex[:12]}"
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
            "input": call.input,
        }
        return f"data: {json.dumps(event)}\n\n"

    # Yield opening role chunk
    yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"

    stdout_done = False
    stdout_iter = cli.stream_call(prompt, model, cwd=cwd, mcp_config_path=mcp_config_path)
    stdout_task: asyncio.Task | None = None

    async def _next_stdout():
        try:
            return await stdout_iter.__anext__()
        except StopAsyncIteration:
            return None

    stdout_task = asyncio.ensure_future(_next_stdout())
    queue_task: asyncio.Task | None = asyncio.ensure_future(session.event_queue.get())

    try:
        while not stdout_done or not session.event_queue.empty():
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
                    else:
                        text = extract_text_from_stream_json(line)
                        if text:
                            yield _text_chunk(text)
                        stdout_task = asyncio.ensure_future(_next_stdout())

                elif task is queue_task:
                    call = task.result()
                    yield _tool_call_event(call)
                    queue_task = asyncio.ensure_future(session.event_queue.get())

    finally:
        # Cancel any remaining tasks
        for t in [stdout_task, queue_task]:
            if t and not t.done():
                t.cancel()

    # Drain remaining queued tool_calls (shouldn't happen, but be safe)
    while not session.event_queue.empty():
        call = session.event_queue.get_nowait()
        yield _tool_call_event(call)

    # Stop chunk
    stop = {
        "id": chat_id, "object": "chat.completion.chunk",
        "created": created, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(stop)}\n\n"
    yield "data: [DONE]\n\n"
```

- [ ] **Step 4.5: Add `uuid` import to server.py**

Add `import uuid` to the imports section at the top of `server.py` (after `import time`).

- [ ] **Step 4.6: Smoke test — start the server and verify it starts cleanly**

```bash
cd C:/Users/MSI_NB/Desktop/ClaudeCLIProxy
python -c "
from claude_cli_proxy.config import ProxyConfig
from claude_cli_proxy.server import create_app
cfg = ProxyConfig()
app = create_app(cfg)
print('Routes:', [str(r.resource) for r in app.router.routes()])
"
```

Expected output includes `/v1/sandbox/results/{call_id}` and `/mcp/{session_id}/sse`.

- [ ] **Step 4.7: Run all proxy tests**

```bash
pytest tests/ -v
```

Expected: All tests pass (no regressions).

- [ ] **Step 4.8: Commit**

```bash
git add claude_cli_proxy/server.py
git commit -m "feat: wire MCP server into proxy, add dual-source SSE generator and tool-result endpoint"
```

---

## Task 5: mobileAgent — Send tools, handle tool_call events, POST results

**Files:**
- Modify: `C:/Users/MSI_NB/Desktop/mobileAgent/src/services/AIService.ts`

- [ ] **Step 5.1: Add sandbox session ID to AIService**

Find the class fields/constructor in `AIService.ts`. Add a stable `_sandboxSessionId` field:

```typescript
// Add with other private fields near the top of the class:
private _sandboxSessionId: string = crypto.randomUUID();
```

If `crypto` is not available in the React Native context, use:
```typescript
import 'react-native-get-random-values';
import { v4 as uuidv4 } from 'uuid';
// ...
private _sandboxSessionId: string = uuidv4();
```

- [ ] **Step 5.2: Modify `streamProxyRaw` — send tools and session header**

Locate `streamProxyRaw` in `AIService.ts` (around line 661). Find where the `body` object is constructed and `headers` is built.

Replace the body construction to include tools and session header when tools are present:

```typescript
// Before (existing — find this block):
const openaiTools = tools?.map(t => ({
  type: 'function' as const,
  function: { name: t.name, description: t.description || '', parameters: t.input_schema || {} },
}));

// ... body construction ...
const body: Record<string, unknown> = {
  model,
  messages: openaiMessages,
  stream: true,
  // ... other fields
};

// After (replace the body construction with):
const openaiTools = tools?.map(t => ({
  type: 'function' as const,
  function: { name: t.name, description: t.description || '', parameters: t.input_schema || {} },
}));

const isSandboxMode = openaiTools && openaiTools.length > 0;

const body: Record<string, unknown> = {
  model,
  messages: openaiMessages,
  stream: true,
  ...(isSandboxMode ? { tools: openaiTools } : {}),
};

// Add session header when in sandbox mode
const requestHeaders: Record<string, string> = {
  'Content-Type': 'application/json',
  ...(isSandboxMode ? { 'x-session-id': this._sandboxSessionId } : {}),
};
```

Then use `requestHeaders` instead of the inline headers object in the `fetch` call.

- [ ] **Step 5.3: Disable guardrail in sandbox mode**

Find where `withProxyGuardrail` is called in `streamProxyRaw` (around line 228 / 661 area). Make it conditional:

```typescript
// Before:
const systemPrompt = this.withProxyGuardrail(options?.systemPrompt);

// After:
const systemPrompt = isSandboxMode
  ? options?.systemPrompt        // sandbox: Claude uses MCP tools, no guardrail needed
  : this.withProxyGuardrail(options?.systemPrompt);
```

Note: `isSandboxMode` must be computed before this line. Reorganize the local variable order if needed.

- [ ] **Step 5.4: Remove `cwd_mode: 'neutral'` in sandbox mode**

Find `cwd_mode: 'neutral'` in the body construction (around line 243). Make it conditional:

```typescript
// Before:
cwd_mode: 'neutral',

// After (in the body object):
...(isSandboxMode ? {} : { cwd_mode: 'neutral' }),
```

- [ ] **Step 5.5: Handle `tool_call` events in the SSE reader**

Find the SSE event reading loop in `streamProxyRaw`. It currently reads `content_block_delta` events. Add handling for `tool_call` events:

```typescript
// Inside the SSE event loop, after parsing `event`:

if (event.type === 'tool_call') {
  const { id: callId, name: toolName, input: toolInput } = event as {
    type: 'tool_call';
    id: string;
    name: string;
    input: Record<string, unknown>;
  };

  // Emit tool_start for UI
  yield { type: 'tool_start', toolName, toolCallId: callId, input: toolInput };

  let toolResult: unknown;
  try {
    // Execute tool in phone sandbox via ToolRegistry
    const tool = this.toolRegistry?.getTool(toolName);
    if (!tool) throw new Error(`Unknown tool: ${toolName}`);
    toolResult = await tool.handler(toolInput);
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);

    // Report error to proxy so MCP call unblocks
    await fetch(`${this.proxyBaseUrl}/v1/sandbox/results/${callId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ error: errMsg }),
    });

    yield { type: 'tool_error', toolName, toolCallId: callId, error: errMsg };
    continue;
  }

  const resultText = typeof toolResult === 'string'
    ? toolResult
    : JSON.stringify(toolResult);

  // Send result back to proxy → MCP handler unblocks → Claude continues
  await fetch(`${this.proxyBaseUrl}/v1/sandbox/results/${callId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ result: resultText }),
  });

  yield { type: 'tool_result', toolName, toolCallId: callId, input: toolInput, result: toolResult, durationMs: 0 };
  continue;
}
```

- [ ] **Step 5.6: Verify `proxyBaseUrl` is accessible within `streamProxyRaw`**

Check that the `fetch` calls in Step 5.5 use the correct base URL. The proxy base URL (e.g., `http://192.168.x.x:8766`) should already be accessible as a field or via config. Find where the chat completions URL is constructed:

```typescript
// Existing fetch call (find this line):
const response = await fetch(`${proxyBaseUrl}/v1/chat/completions`, ...);
```

Use the same `proxyBaseUrl` variable in the tool result POST. If it's not a variable yet, extract it:

```typescript
const proxyBaseUrl = this.config.proxyUrl.replace(/\/v1\/?$/, '');
// Use proxyBaseUrl in both the chat completions fetch and the tool result fetch
```

- [ ] **Step 5.7: Test end-to-end with a simple read_file tool call**

With the proxy running (`python -m claude_cli_proxy` or `python run.py`), and a mobileAgent dev build:

1. Open mobileAgent on phone/simulator
2. Open an existing project in the sandbox
3. Ask Claude: "What's in the first file you can see?"
4. Observe in proxy logs: `[MCP] tool_call list_directory → waiting for phone`
5. Observe in mobileAgent: tool_start event fires, list_directory executes, result POSTs to proxy
6. Observe Claude's response uses actual sandbox file names

Check proxy logs show: `session=<uuid> tools=4 (MCP bridge)`

- [ ] **Step 5.8: Commit mobileAgent changes**

```bash
cd C:/Users/MSI_NB/Desktop/mobileAgent
git add src/services/AIService.ts
git commit -m "feat: send tools to proxy, handle tool_call SSE events, POST sandbox results"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| SessionRegistry (register/get/unregister/add_pending_call/resolve/reject) | Task 1 |
| MCP SSE server (GET /sse, POST /messages, initialize/tools/list/tools/call) | Task 2 |
| `mcp_server_port`, `tool_call_timeout` config | Task 3 |
| `stream_call()` async generator, `--mcp-config` arg | Task 3 |
| `extract_text_from_stream_json()` | Task 3 |
| `handle_tool_result` endpoint | Task 4 |
| `_write_mcp_config()` per-session file | Task 4 |
| Dual-source SSE generator | Task 4 |
| MCP wiring in `create_app()` | Task 4 |
| Send tools + x-session-id from phone | Task 5 |
| Disable guardrail in sandbox mode | Task 5 |
| Remove `cwd_mode: neutral` in sandbox mode | Task 5 |
| Handle `tool_call` SSE event, execute, POST result | Task 5 |

**Placeholder scan:** No TBDs. All code blocks are complete. All `expected:` outputs are concrete.

**Type consistency:**
- `PendingToolCall.id` → used as `call.id` throughout ✓
- `session.event_queue` → `asyncio.Queue` of `PendingToolCall` objects ✓
- `_response_queues[session_id]` → `asyncio.Queue` of `dict` (JSON-RPC response) ✓
- `tool_call` SSE event shape: `{type, id, name, input}` — matches in server generator and mobileAgent reader ✓
- Tool result POST body: `{result: string}` or `{error: string}` — matches `handle_tool_result` ✓
