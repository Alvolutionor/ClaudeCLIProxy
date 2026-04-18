# Claude CLI Proxy

OpenAI-compatible API server that wraps the [Claude CLI](https://docs.anthropic.com/en/docs/claude-code). Use your Claude CLI subscription as an API — no extra API costs.

## When to use this

Built for one scenario: **remote/mobile client drives your local Claude CLI, with the client's own tools executed on the client side** via an MCP bridge.

- Spawns the official `claude` subprocess per request (requires Claude CLI installed and logged in)
- Tools are executed by the remote client, not the desktop — the CLI's built-in FS/exec tools are disabled
- Think of it as a remote-control harness for your local Claude CLI, not a multi-provider gateway

## Quick Start

```bash
pip install -r requirements.txt
python run.py
```

Server starts at `http://localhost:8766/v1`. CLI runs in a neutral temp dir by default, so the proxy repo's own context never leaks in.

## Usage

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8766/v1", api_key="not-needed")
resp = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Streaming (`stream=True`), curl, and `fetch()` all work the same way.

## Request Options

`POST /v1/chat/completions` also accepts:

- `tools` — OpenAI-style tool defs; when present, the proxy starts an MCP bridge and Claude calls **your** tools instead of its built-ins
- `cwd_mode` — `"neutral"` (default) or `"inherit"`
- `cwd` — explicit directory to run CLI from
- `open_file`, `diagnostics`, `prefetch` — request-level context

Headers:

- `x-session-id` — stable conversation ID, enables `--resume` across turns
- `x-session-meta` — JSON blob of long-lived session metadata
- `x-refresh-context: true` — invalidate cached env snapshot

## Endpoints

| Method | Path | |
|---|---|---|
| POST | `/v1/chat/completions` | chat completion |
| POST | `/v1/sandbox/results/{call_id}` | remote client submits tool result |
| GET | `/v1/models` | list models |
| GET | `/health` | health check |

Models: `claude-haiku-4-5-20251001`, `claude-sonnet-4-6`, `claude-opus-4-6`

## Sandbox

The CLI subprocess is the only thing that could execute anything, so:

- `--disallowedTools` blocks every built-in FS/exec/net tool; only `mcp__*` bridge tools pass
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `..._AUTO_MEMORY=1`, `..._GIT_INSTRUCTIONS=1` — no CLAUDE.md, memory, or git context leaks
- Neutral temp cwd by default
- MCP endpoint scoped per session, torn down on request end

## CLI Options

```
--host / --port           bind address / port (default 0.0.0.0:8766)
--model                   default model (default claude-haiku-4-5-20251001)
--max-concurrent          max concurrent CLI calls (default 3)
--timeout                 CLI call timeout seconds (default 300)
--retry-count / --retry-delay
--git-bash-path           path to git bash (auto-detected)
```

## Requirements

Python 3.10+, [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated, `aiohttp`.

## License

MIT
