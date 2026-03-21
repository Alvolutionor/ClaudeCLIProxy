# Claude CLI Proxy

OpenAI-compatible API server that wraps the [Claude CLI](https://docs.anthropic.com/en/docs/claude-code). Use your Claude CLI subscription as an API — no extra API costs.

## Why

- You have a Claude CLI subscription (e.g., Claude Code)
- You want to use it from your own apps (Expo, web, scripts) via standard OpenAI SDK
- You don't want to pay for separate API access

## Quick Start

```bash
git clone <this-repo>
cd claude-cli-proxy
pip install -r requirements.txt
python run.py
```

Server starts at `http://localhost:8766/v1`.

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
  }),
});
const data = await response.json();
console.log(data.choices[0].message.content);
```

## CLI Options

```
python run.py [OPTIONS]

--host             Bind address (default: 0.0.0.0)
--port             Server port (default: 8766)
--model            Default model (default: claude-haiku-4-5-20251001)
--max-concurrent   Max concurrent CLI calls (default: 3)
--timeout          CLI call timeout in seconds (default: 300)
--git-bash-path    Path to git bash executable
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

## Project Structure

```
claude_cli_proxy/
├── __init__.py          # Package version
├── config.py            # Configuration management
├── cli.py               # Claude CLI subprocess wrapper
├── openai_compat.py     # OpenAI protocol conversion
└── server.py            # HTTP routes and server
run.py                   # Entry point
```

## License

MIT
