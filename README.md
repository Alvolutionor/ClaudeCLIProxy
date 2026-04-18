# Claude CLI Proxy

OpenAI-compatible API server that wraps the [Claude CLI](https://docs.anthropic.com/en/docs/claude-code). Use your Claude CLI subscription as an API — no extra API costs.

## Why this project (and when not to use it)

This project is **narrow and deep**, not a general-purpose LLM gateway. It exists for one scenario: letting a **remote/mobile client drive a Claude Code session running on your desktop**, with the client's own tools executed on the client side via an MCP bridge.

If you want a multi-backend gateway (Claude + Gemini + Codex + OpenAI upstreams, OAuth multi-account pool, format translation), use [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) instead — it is a broader, production-grade router.

### Use this project when you want

- **Phone / remote app → desktop Claude CLI**, with tools (read file, list dir, run command, etc.) executed **on the phone/remote**, not on the desktop
- A trusted **official-CLI path** (no reverse-engineered OAuth client IDs, no TLS fingerprint spoofing — the subprocess *is* the officially supported client)
- To keep every Claude CLI subfeature Anthropic ships (hooks, `--resume` sessions, thinking, CLAUDE.md toggles, MCP) without re-implementing anything
- Hard context isolation: your global `~/.claude/CLAUDE.md`, memory, and git context are **never** leaked into remote requests

### Use CLIProxyAPI instead when you want

- Multiple LLM backends behind one endpoint
- Multi-account OAuth pool with round-robin load balancing
- Zero-subprocess, pure-HTTP proxying (lower latency, no CLI install)
- Format translation between OpenAI / Gemini / Claude / Codex protocols

### Architecture difference in one line

| | This project | CLIProxyAPI |
|---|---|---|
| How it talks to Claude | spawns `claude` subprocess per request | direct HTTPS to `api.anthropic.com` with OAuth token |
| Requires `claude` CLI installed | yes | no |
| Tools are executed by | the **remote client** (via MCP bridge) | the downstream client app (Cursor/Cline/etc.) |
| Sandbox concern | real — hence `--disallowedTools` + env scrubbing + neutral cwd | none — proxy never executes |
| Analogy | a remote-control harness for your local Claude CLI | a multi-provider HTTP reverse proxy |

## Why

- You have a Claude CLI subscription (e.g., Claude Code)
- You want to use it from your own apps (Expo, web, scripts) via standard OpenAI SDK
- You don't want to pay for separate API access
- You want to drive it from your phone and have tools run on the phone, not on the desktop

## Quick Start

```bash
git clone <this-repo>
cd claude-cli-proxy
pip install -r requirements.txt
python run.py
```

Server starts at `http://localhost:8766/v1`.

By default the proxy now runs Claude CLI from a neutral temp directory, so it does not accidentally inject the proxy repo's own git/working-directory context into requests.

## Usage

### From any OpenAI-compatible client

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8766/v1",
    api_key="not-needed",
)

response = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### From curl

```bash
curl http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### From Expo / React Native

```javascript
const response = await fetch("http://<your-ip>:8766/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "claude-sonnet-4-6",
    messages: [{ role: "user", content: "Hello!" }],
    cwd_mode: "neutral"
  }),
});
const data = await response.json();
console.log(data.choices[0].message.content);
```

### Streaming

```python
stream = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## CLI Options

```
python run.py [OPTIONS]

--host             Bind address (default: 0.0.0.0)
--port             Server port (default: 8766)
--model            Default model (default: claude-haiku-4-5-20251001)
--max-concurrent   Max concurrent CLI calls (default: 3)
--timeout          CLI call timeout in seconds (default: 300)
--git-bash-path    Path to git bash executable (auto-detected)
--retry-count      Number of retries on failure (default: 2)
--retry-delay      Delay between retries in seconds (default: 1.0)
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/chat/completions` | OpenAI-compatible chat completion |
| GET | `/v1/models` | List available models |
| GET | `/health` | Health check |

## Available Models

- `claude-haiku-4-5-20251001`
- `claude-sonnet-4-6`
- `claude-opus-4-6`

## Requirements

- Python 3.10+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- `aiohttp`

## Request Options

`POST /v1/chat/completions` also accepts these optional fields:

- `cwd_mode`: `"neutral"` (default) or `"inherit"`
- `cwd`: explicit directory to run Claude CLI from
- `tools`: OpenAI-style tool definitions — when present, the proxy starts an MCP bridge so the Claude subprocess calls **your** tools (not its built-in Bash/Read/Write)
- `open_file`, `diagnostics`, `prefetch`: request-level context forwarded to the model

Use `cwd` only when you intentionally want Claude CLI to see a specific desktop workspace. For mobile/remote clients, keep the default `cwd_mode: "neutral"` and send runtime context explicitly in your prompt.

Request headers:

- `x-session-id`: stable per-conversation ID — enables `--resume` across turns
- `x-session-meta`: JSON blob of long-lived session metadata (e.g. `cwd`)
- `x-refresh-context: true`: invalidate the cached environment snapshot for this session

## Sandbox / Isolation

The CLI subprocess is the only surface that could execute anything, so it is locked down:

- **Tool blacklist**: `--disallowedTools Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,NotebookRead,NotebookEdit,Task,Agent` — every built-in filesystem/exec/network tool is disabled. Only `mcp__*` tools (the bridge to your remote client) pass through.
- **Environment scrub**: `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` — no CLAUDE.md, no `~/.claude/projects/` memory, no git metadata leaks into the remote request.
- **Neutral cwd**: by default the subprocess runs in a temp directory so it cannot see the proxy repo itself or any user workspace.
- **MCP session scope**: each request gets a per-session MCP endpoint on `localhost`, tools are registered dynamically from the request, and the session is torn down when the request ends.

## Project Structure

```
claude_cli_proxy/
├── __init__.py          # Package version
├── config.py            # Configuration management
├── cli.py               # Claude CLI subprocess wrapper (with sandbox flags)
├── openai_compat.py     # OpenAI protocol conversion
├── server.py            # HTTP routes and server
├── mcp_server.py        # MCP SSE bridge for remote-client tools
├── session_registry.py  # Per-session tool registry + context cache
├── context_builder.py   # Env snapshot assembly + pre-fetch orchestration
└── conversation_store.py # x-session-id → Claude --resume mapping
run.py                   # Entry point
```

## License

MIT
