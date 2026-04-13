# spekoai-mcp

Model Context Protocol server for [SpekoAI](https://speko.ai) — a thin wrapper
over the [`spekoai`](https://pypi.org/project/spekoai/) Python SDK that exposes
voice-AI session and usage tools to MCP clients (Claude Desktop, Cursor, etc.).

A hosted version is available at `https://mcp.speko.ai`. You can also run it
locally over stdio for development.

## Tools

| Tool | Description |
| --- | --- |
| `create_session` | Create a new voice session. |
| `get_session` | Fetch a session by id. |
| `end_session` | End an active session. |
| `get_usage_summary` | Get usage summary for the current billing period. |

## Local development (stdio)

```bash
uv run --with spekoai-mcp -- spekoai-mcp --stdio
```

Set `SPEKOAI_API_KEY` (and optionally `SPEKOAI_BASE_URL`) in the environment.

Wire into Claude Desktop's MCP config:

```json
{
  "mcpServers": {
    "spekoai": {
      "command": "uv",
      "args": ["run", "--with", "spekoai-mcp", "--", "spekoai-mcp", "--stdio"],
      "env": { "SPEKOAI_API_KEY": "sk_test_..." }
    }
  }
}
```

## HTTP (production)

HTTP mode requires OAuth. Set:

- `SPEKOAI_OAUTH_ISSUER`
- `SPEKOAI_OAUTH_CLIENT_ID`
- `SPEKOAI_OAUTH_CLIENT_SECRET`
- `SPEKOAI_MCP_BASE_URL` (defaults to `https://mcp.speko.ai`)

Then:

```bash
uv run spekoai-mcp                  # HTTP on 0.0.0.0:8000
uv run spekoai-mcp --host 127.0.0.1 --port 9000
```

The server fails to start in HTTP mode if OAuth env vars are missing — this
prevents accidentally serving an unauthenticated public endpoint.
