# MCP Tool Bridge Design
**Date:** 2026-04-16  
**Project:** ClaudeCLIProxy → mobileAgent sandbox tool bridge  
**Status:** Approved for implementation

---

## Goal

Make ClaudeCLIProxy a true mobile sandbox tool bridge: when mobileAgent calls the proxy,
Claude's reasoning happens on the desktop, but every file/search/terminal tool is executed
inside the phone's sandbox via mobileAgent's ToolRegistry. The proxy only forwards tool
calls and results — it never executes tools itself.

---

## Architecture

### Data Flow

```
Phone (mobileAgent)                     ClaudeCLIProxy (Desktop)
────────────────────                    ──────────────────────────────────────

AIService.streamProxyRaw()
  │
  ├─ POST /v1/chat/completions ────────► handle_chat_completions()
  │   headers: x-session-id: S1         ├─ register_session(S1, tools)
  │   body: {messages, tools,           ├─ write /tmp/mcp-S1.json
  │           stream:true}              ├─ spawn: claude -p
  │                                     │    --mcp-config /tmp/mcp-S1.json
  │                                     │    --output-format stream-json
  │                                     │
  │                                     │  Claude CLI ──MCP JSON-RPC──► MCP Server
  │                                     │  Claude calls: tools/call read_file
  │                                     │  → session.event_queue.put(call)
  │                                     │  → MCP handler blocks waiting for result
  │                                     │
  │ ◄── SSE: tool_call event ──────────│  generate_sse() monitors both:
  │  {type:"tool_call",                 │  ① Claude stdout (text output)
  │   id:"tc1", name:"read_file",       │  ② session.event_queue (tool calls)
  │   input:{path:"src/app.tsx"}}       │  Interleaved into single SSE stream
  │
  ├─ ToolRegistry.execute("read_file")
  │   result = "import React..."
  │
  ├─ POST /v1/sandbox/results/tc1 ─────► handle_tool_result()
  │   {result: "import React..."}        └─ session.resolve("tc1", result)
  │                                           └─ MCP handler unblocks
  │                                           └─ Returns result to Claude CLI
  │                                           └─ Claude continues generating
  │
  │ ◄── SSE: content_block_delta ──────│  Claude stdout → SSE stream
  │  {delta:{text:"The file contains..."}}
  │
  │ ◄── SSE: message_stop ─────────────│  Claude done
```

### Core Principles (from Antigravity analysis)

1. **Proxy is stateless per request** — session state lives only for the duration of one
   `POST /v1/chat/completions` call; cleaned up on completion or error.
2. **Phone maintains full conversation history** — messages array includes all prior
   `tool_use` and `tool_result` blocks. Proxy never reconstructs history.
3. **MCP is the native Claude tool protocol** — no prompt engineering, no XML parsing.
   Claude CLI calls tools via MCP JSON-RPC; proxy routes calls to phone.
4. **SSE stream is unified** — text deltas and tool_call events flow through the same
   stream. Phone handles both event types.

---

## Components

### 1. SessionRegistry (`claude_cli_proxy/session_registry.py`) — NEW

Manages per-request session state. Lifetime: from `POST /v1/chat/completions` to
request completion or error.

```python
@dataclass
class PendingToolCall:
    id: str
    name: str
    input: dict
    result_event: asyncio.Event
    result: str | None = None
    error: str | None = None

@dataclass  
class SessionState:
    session_id: str
    tools: list[dict]            # OpenAI-format tool schemas from phone
    event_queue: asyncio.Queue   # tool_call events waiting to be sent to phone
    pending_calls: dict[str, PendingToolCall]  # call_id → PendingToolCall
    created_at: float
```

**API:**
- `register(session_id, tools) → SessionState`
- `get(session_id) → SessionState`
- `unregister(session_id)`
- `add_pending_call(session_id, call_id, name, input) → PendingToolCall`
- `resolve_call(call_id, result)` — called by tool-result endpoint
- `reject_call(call_id, error)` — called on timeout

### 2. MCP Server (`claude_cli_proxy/mcp_server.py`) — NEW

Single aiohttp server on a fixed port (default 18766). Implements MCP SSE transport:

**Routes:**
- `GET /mcp/{session_id}/sse` — Claude CLI connects here; SSE stream for JSON-RPC responses
- `POST /mcp/{session_id}/messages` — Claude CLI posts JSON-RPC requests here

**JSON-RPC methods handled:**

| Method | Handler |
|--------|---------|
| `initialize` | Returns server capabilities |
| `tools/list` | Returns tools from SessionRegistry for this session_id |
| `tools/call` | Creates PendingToolCall, puts in event_queue, blocks until resolved |

**tools/call flow:**
```python
async def handle_tools_call(session_id, call_id, name, arguments):
    call = session_registry.add_pending_call(session_id, call_id, name, arguments)
    
    # Notify SSE stream that there's a tool call waiting
    session = session_registry.get(session_id)
    await session.event_queue.put(call)
    
    # Block until phone posts result (or timeout)
    try:
        await asyncio.wait_for(call.result_event.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        return {"error": {"code": -32000, "message": "Tool call timed out"}}
    
    if call.error:
        return {"error": {"code": -32001, "message": call.error}}
    
    return {"content": [{"type": "text", "text": call.result}]}
```

**MCP config file written per-request** (`/tmp/mcp-{session_id}.json`):
```json
{
  "mcpServers": {
    "mobile-sandbox": {
      "type": "sse",
      "url": "http://localhost:18766/mcp/{session_id}/sse"
    }
  }
}
```

### 3. CLI Invocation (`claude_cli_proxy/cli.py`) — MODIFY

Add `mcp_config_path` parameter to `call()`. When provided:
- Pass `--mcp-config {path}` to Claude CLI subprocess
- Optionally add `--allowedTools` to restrict to only MCP tools
- Clean up config file after subprocess exits

```python
async def call(self, prompt, model, cwd=None, mcp_config_path=None):
    args = ["claude", "-p", "--model", model, "--output-format", "stream-json"]
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path]
    # ... rest of subprocess logic
```

### 4. Server Routes (`claude_cli_proxy/server.py`) — MODIFY

**Modified: `POST /v1/chat/completions`**
- Accept `tools` array (OpenAI format) in request body
- Accept `x-session-id` header (auto-generate UUID if absent)
- Register session with tools before spawning Claude CLI
- Pass `mcp_config_path` to `cli.call()` when tools are present
- Unregister session in `finally` block

**New: `POST /v1/sandbox/results/{call_id}`**
```python
async def handle_tool_result(request):
    call_id = request.match_info['call_id']
    body = await request.json()
    result = body.get('result', '')
    error = body.get('error')
    
    if error:
        session_registry.reject_call(call_id, error)
    else:
        session_registry.resolve_call(call_id, result)
    
    return web.Response(status=204)
```

### 5. SSE Stream Generator (`claude_cli_proxy/server.py`) — MODIFY

Replaces the current simple stdout-read loop with a dual-source async generator:

```python
async def generate_sse(session_id, process, session):
    stdout_done = False
    
    while not stdout_done or not session.event_queue.empty():
        stdout_task = asyncio.ensure_future(process.stdout.readline())
        queue_task  = asyncio.ensure_future(session.event_queue.get())
        
        done, pending = await asyncio.wait(
            [stdout_task, queue_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in pending:
            task.cancel()
        
        for task in done:
            if task is stdout_task:
                line = task.result()
                if not line:
                    stdout_done = True
                else:
                    # Convert stream-json line to SSE chunk (existing logic)
                    yield f"data: {convert_stream_json(line)}\n\n"
            
            elif task is queue_task:
                call = task.result()
                # Emit tool_call event to phone
                yield f"data: {json.dumps({
                    'type': 'tool_call',
                    'id': call.id,
                    'name': call.name,
                    'input': call.input
                })}\n\n"
    
    yield "data: [DONE]\n\n"
```

### 6. OpenAI Compat (`claude_cli_proxy/openai_compat.py`) — MODIFY

Add `tool_call` as a passthrough event type in the SSE format. The existing
`build_stream_chunks()` handles text; `tool_call` events bypass that function
and are emitted directly from the stream generator.

---

## Protocol Changes: mobileAgent Side

### `src/services/AIService.ts` — MODIFY

**A. Remove guardrail for sandbox mode**

Replace `withProxyGuardrail` logic in `callClaudeProxy` / `streamProxyRaw`:
```typescript
// Before (always applies guardrail):
systemPrompt = this.withProxyGuardrail(systemPrompt);

// After (conditional on mode):
if (!sandboxMode) {
  systemPrompt = this.withProxyGuardrail(systemPrompt);
}
```

Sandbox mode is detected by presence of tools in the request.

**B. Send tools with proxy request**

In `streamProxyRaw`, when tools are provided:
```typescript
const body = {
  model,
  messages: openaiMessages,
  stream: true,
  tools: openaiTools,   // already converted by existing toApiTools() logic
  tool_choice: 'auto',
};
headers['x-session-id'] = this.sandboxSessionId; // UUID, stable per AIService instance
```

**C. Handle tool_call events in SSE stream**

Extend the existing SSE reader in `streamProxyRaw`:
```typescript
if (event.type === 'tool_call') {
  // Execute tool via ToolRegistry
  const result = await this.toolRegistry.execute(event.name, event.input);
  
  // Post result back to proxy
  await fetch(`${proxyBaseUrl}/v1/sandbox/results/${event.id}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ result: JSON.stringify(result) })
  });
  
  // Emit tool events for UI
  this.emit({ type: 'tool_start', toolName: event.name, toolCallId: event.id, input: event.input });
  this.emit({ type: 'tool_result', toolName: event.name, toolCallId: event.id, result });
}
```

**D. Remove `cwd_mode: 'neutral'` for sandbox requests**

When tools are present, omit `cwd_mode` entirely (Claude CLI will use the MCP context).

---

## SSE Event Reference

| Event type | Direction | Meaning |
|------------|-----------|---------|
| `content_block_start` | proxy → phone | Claude starting a text block |
| `content_block_delta` | proxy → phone | Text chunk from Claude |
| `content_block_stop` | proxy → phone | Text block complete |
| `message_stop` | proxy → phone | Claude done, no more tool calls |
| `tool_call` | proxy → phone | Claude wants to call a tool (phone must execute + POST result) |

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Phone doesn't POST result within 60s | MCP handler rejects call; Claude gets error; Claude reports to phone via text |
| Claude CLI crashes mid-session | SSE stream ends with error event; session cleaned up |
| Tool execution fails on phone | Phone POSTs `{error: "..."}` instead of `{result: ...}`; MCP returns error to Claude |
| Session not found for tool-result POST | 404 response; phone logs warning |
| Multiple concurrent tool calls | Each has unique `call_id`; all go into same `event_queue`; resolved independently |

---

## Configuration (`claude_cli_proxy/config.py`) — MODIFY

Add:
```python
mcp_server_port: int = 18766   # Port for internal MCP server
mcp_config_dir: str = "/tmp"   # Directory for MCP config temp files
tool_call_timeout: int = 60    # Seconds to wait for phone tool result
```

---

## Files Changed

### ClaudeCLIProxy
| File | Change |
|------|--------|
| `claude_cli_proxy/session_registry.py` | NEW — session state management |
| `claude_cli_proxy/mcp_server.py` | NEW — MCP SSE server + JSON-RPC handlers |
| `claude_cli_proxy/cli.py` | MODIFY — add `--mcp-config` support |
| `claude_cli_proxy/server.py` | MODIFY — accept tools, dual-source SSE, tool-result endpoint |
| `claude_cli_proxy/openai_compat.py` | MODIFY — passthrough tool_call event type |
| `claude_cli_proxy/config.py` | MODIFY — MCP port, timeout config |

### mobileAgent
| File | Change |
|------|--------|
| `src/services/AIService.ts` | MODIFY — send tools, handle tool_call SSE events, POST results |

---

## Out of Scope

- write_file / create_file support (mobileAgent already has this gap; separate issue)
- MCP authentication between Claude CLI and our internal MCP server (loopback only)
- Persistent session state across proxy restarts
- Multiple simultaneous phone clients per proxy instance
