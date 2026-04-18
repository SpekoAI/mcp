# spekoai-mcp

Model Context Protocol server for [SpekoAI](https://speko.ai) — a thin wrapper
over the [`spekoai`](https://pypi.org/project/spekoai/) Python SDK that exposes
voice-AI session and usage tools to MCP clients (Claude Desktop, Cursor, etc.).

A hosted version is available at `https://mcp.speko.ai`.

## Tools

| Tool | Description |
| --- | --- |
| `get_usage_summary` | Get usage summary for the current billing period. |

The tool surface mirrors `spekoai.AsyncSpekoAI` — more tools land here as
the SDK grows.

## Auth model

The server has no long-lived SpekoAI credential of its own. Every tool
call forwards the caller's OAuth access token (minted by the Better Auth
`oauthProvider` plugin on the platform) straight to the SpekoAI API,
which validates the JWT and scopes the call to the caller's user and
organization. There is no `SPEKOAI_API_KEY`.

## Running

HTTP-only. Set:

- `SPEKOAI_OAUTH_ISSUER` — must end in `/oauth2`
- `SPEKOAI_OAUTH_CLIENT_ID`
- `SPEKOAI_OAUTH_CLIENT_SECRET`
- `SPEKOAI_MCP_BASE_URL` — public URL of this server (no default; fail-closed)
- `SPEKOAI_OAUTH_AUDIENCE` — optional; defaults to `${SPEKOAI_MCP_BASE_URL}/mcp` (the MCP resource URL per RFC 8707). Must also appear in the platform's `SPEKOAI_OAUTH_VALID_AUDIENCES` allowlist — otherwise Better Auth rejects the authorize request
- `SPEKOAI_BASE_URL` — optional upstream override (default `https://api.speko.ai`)

```bash
uv run spekoai-mcp                  # HTTP on 0.0.0.0:8080
uv run spekoai-mcp --host 127.0.0.1 --port 9000
```

The server fails to start if any required env var is missing — this
prevents accidentally serving an unauthenticated or mis-targeted endpoint.

### Deriving the OAuth env vars

The SpekoAI platform is its own OAuth 2.1 / OIDC issuer (Better Auth's
`@better-auth/oauth-provider` plugin, see `apps/server/src/lib/auth.ts`).
The endpoints live under `/api/auth/oauth2/*` on the dashboard origin,
which rewrites through to the server.

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
    --redirect-uri https://mcp-staging.speko.dev/auth/callback
```

The redirect URI must match FastMCP `OAuthProxy`'s callback path, which is
`/auth/callback` (not `/oauth/callback`). Platform-staging will reject the
upstream authorize request with `invalid_redirect` if the registered URI
doesn't match.

The script prints:

```json
{ "client_id": "...", "client_secret": "..." }
```

The secret is returned **once** — store it somewhere durable (Cloud Run
secret, 1Password, etc.). You can also hit the public endpoint directly:

```bash
curl -X POST https://platform-staging.speko.dev/api/auth/oauth2/register \
  -H "Content-Type: application/json" \
  -H "Origin: https://platform-staging.speko.dev" \
  -H "Cookie: __Secure-better-auth.session_token=<paste_your_cookie_val>" \
  -d '{
    "client_name": "SpekoAI MCP (staging)",
    "redirect_uris": ["https://mcp-staging.speko.dev/auth/callback"],
    "token_endpoint_auth_method": "client_secret_basic",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "scope": "openid profile email"
  }'
```

`allowUnauthenticatedClientRegistration: true` only opens registration for
**public** clients (`token_endpoint_auth_method: "none"`, PKCE-only).
Registering a **confidential** client via the public endpoint requires a
logged-in user session — hence the `Cookie` and `Origin` headers above.
Authenticate at the dashboard, grab the session cookie from DevTools, then
POST. Client secrets are stored hashed, so a DB breach doesn't leak
plaintext.

If you already have a client row and only need to tweak its redirect URIs
(e.g. adding a local MCP Inspector callback for testing), patch the row
directly — the public endpoint is create-only:

```sql
UPDATE oauth_client
SET redirect_uris = array_append(redirect_uris, 'https://mcp-staging.speko.dev/auth/callback')
WHERE client_id = '<client-id>';
```

Once the MCP client is registered, add its `client_id` to
`SPEKOAI_TRUSTED_CLIENT_IDS` on the server so users skip the consent screen
for this first-party client. OAuth 2.1 requires PKCE, which FastMCP
`OAuthProxy` performs automatically.
