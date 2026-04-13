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

### Deriving the OAuth env vars

The SpekoAI platform is its own OIDC issuer (Better Auth's `oidcProvider`
plugin, see `apps/server/src/lib/auth.ts`). The endpoints live under
`/api/auth/oauth2/*` on the dashboard origin, which rewrites through to the
server.

For a deployment where the dashboard is `https://platform.speko.ai`:

```
SPEKOAI_OAUTH_ISSUER=https://platform.speko.ai/api/auth/oauth2
SPEKOAI_MCP_BASE_URL=https://mcp.speko.ai
```

**Note: two different "issuer" values.** FastMCP's `OAuthProxy` appends
`/authorize` and `/token` to `SPEKOAI_OAUTH_ISSUER`, so this env var must
end at the `/oauth2` segment — no trailing slash. That is **not** the OIDC
spec `iss` claim. The OIDC spec issuer (and the `iss` value inside tokens
emitted by the platform) is `https://platform.speko.ai/api/auth` — one
segment shorter, matching where Better Auth mounts the discovery document.
Same host, different paths.

To mint `SPEKOAI_OAUTH_CLIENT_ID` / `SPEKOAI_OAUTH_CLIENT_SECRET`, register
the MCP server as an OAuth client against the platform. From a checkout of
`github.com/SpekoAI/platform`, with `apps/server/.env` pointing at the
target database:

```bash
bun --env-file=apps/server/.env \
    apps/server/scripts/register-oauth-client.ts \
    --name "SpekoAI MCP (staging)" \
    --redirect-uri https://mcp-staging.speko.dev/oauth/callback
```

The script prints:

```json
{ "client_id": "...", "client_secret": "..." }
```

The secret is returned **once** — store it somewhere durable (Cloud Run
secret, 1Password, etc.). You can also hit the public endpoint directly:

```bash
curl -X POST https://platform.speko.ai/api/auth/oauth2/register \
  -H "Content-Type: application/json" \
  -d '{
    "client_name": "SpekoAI MCP (staging)",
    "redirect_uris": ["https://mcp-staging.speko.dev/oauth/callback"],
    "token_endpoint_auth_method": "client_secret_basic",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "scope": "openid profile email"
  }'
```

`allowDynamicClientRegistration: true` in the plugin config is what makes
this endpoint publicly callable — convenient for FastMCP's first-run
self-registration, but worth gating behind an allowlist hook once we have
more than one client in production. Client secrets are stored hashed
(`storeClientSecret: 'hashed'`), so a DB breach doesn't leak plaintext.
