# spekoai-mcp

Model Context Protocol server for [SpekoAI](https://speko.ai) — the
authoritative source for SpekoAI's SDKs, adapters, and platform via
MCP. Designed so an agent (Claude Code, Claude Desktop, Cursor) can
authenticate once and then answer any SpekoAI question or scaffold a
new project without external lookups.

A hosted version is available at `https://mcp.speko.ai`.

## Surfaces

### Resources — bundled product docs

Every SDK/adapter's README and `SKILLS.md`, plus `CLAUDE.md` and the
Node quickstart, ship inside the wheel as MCP resources.

- `spekoai://docs/index` — start here; lists every bundled doc with a
  one-line summary.
- `spekoai://docs/{slug}` — open a specific doc. Slugs include
  `sdk-skills`, `sdk-readme`, `client-skills`, `client-readme`,
  `sdk-python-skills`, `sdk-python-readme`,
  `adapter-livekit-skills`, `adapter-livekit-readme`,
  `adapter-vapi-skills`, `adapter-vapi-readme`,
  `adapter-retell-skills`, `adapter-retell-readme`,
  `mcp-server-readme`, `quickstart-node-readme`,
  `quickstart-node-index-ts`.

Only public, user-facing docs are bundled. Internal packages
(`@spekoai/core`, `@spekoai/providers`), the monorepo-level
`CLAUDE.md`, and per-package `ROADMAP.md` files are intentionally
excluded — they describe internal architecture or forward-looking
product direction that shouldn't leak through a publicly-reachable
MCP.

Skill sheets are dense, LLM-oriented references (API surface, minimal
snippets, common gotchas). READMEs are the longer prose walkthroughs.

### Components — copy-paste client snippets

Drop-in frontend components wrapping the SpekoAI SDKs. Mime type is
`text/plain` so clients don't mangle the source during re-emission.

- `spekoai://components/react/voice-session` — `<SpekoVoiceSession>`
  React component wrapping `@spekoai/client`'s `VoiceConversation.create()`.
  Marked `'use client'` for Next.js App Router; dynamic-imports the SDK
  so it stays out of the SSR bundle.

### Prompts

| Prompt | Args | Description |
| --- | --- | --- |
| `scaffold_project` | `scenario`, `language?`, `runtime?` | Step-by-step scaffold. Scenarios: `voice_conversation`, `batch_transcribe`, `livekit_agent`, `quickstart`. `voice_conversation` and `livekit_agent` are TypeScript-only. |

### Tools

| Tool | Description |
| --- | --- |
| `search_docs` | Full-text search over bundled SpekoAI docs. Returns slug + snippet + score. |
| `list_packages` | Structured manifest of every SpekoAI package with URIs to its README / SKILLS sheet. |
| `recommended_stack` | Opinionated SpekoAI stack for one Speko use case (`general`, `healthcare`, `finance`, `legal`). Returns packages, tagline, use-case-specific rationale and compliance warnings, and a handoff to `scaffold_voice_app`. |
| `scaffold_voice_app` | Strict Next.js App Router scaffold manifest for a browser voice app. Args: `use_case`, `languages?` (`en`/`es`, default `['en']`), `system_prompt?` (overrides the use-case default). Emits four files (route handler, React component, page, `.env.example`) plus install commands and env vars. |
| `get_balance` | Caller's current prepaid credit balance (`balance_micro_usd`, `balance_usd`, `updated_at`). Requires OAuth; forwards the access token to `api.speko.ai/v1/credits/balance`. |

The knowledge surface (resources, prompts, `search_docs`,
`list_packages`, `recommended_stack`, `scaffold_voice_app`) ships
static bundled data and needs no credentials. `get_balance` is an
action tool that calls `api.speko.ai` on the caller's behalf, so it
requires an OAuth-capable client and the server to be launched with
the four `SPEKOAI_OAUTH_*` env vars set — see the `Auth model` section
below.

## Auth model

The server has no long-lived SpekoAI credential of its own. Action
tools forward the caller's OAuth access token (minted by the Better
Auth `oauthProvider` plugin on the platform) straight to the SpekoAI
API, which validates the JWT and scopes the call to the caller's user
and organization. There is no `SPEKOAI_API_KEY`.

`get_balance` is the first such action tool. With OAuth configured,
the MCP client mints an access token against the platform, FastMCP's
`OAuthProxy` verifies it, and the tool forwards the same JWT to the
SpekoAI API via `Authorization: Bearer <token>`. With OAuth
unconfigured the server still boots — the knowledge surface stays
public — but `get_balance` raises a `ToolError` telling the caller to
set the `SPEKOAI_OAUTH_*` env vars.

## Running

HTTP-only.

```bash
uv run spekoai-mcp                  # HTTP on 0.0.0.0:8080
uv run spekoai-mcp --host 127.0.0.1 --port 9000
```

No env vars are required to run. When you re-introduce OAuth-gated
tools, set the four vars below and the CLI mounts `OAuthProxy`
automatically:

- `SPEKOAI_OAUTH_ISSUER` — must end in `/oauth2`
- `SPEKOAI_OAUTH_CLIENT_ID`
- `SPEKOAI_OAUTH_CLIENT_SECRET`
- `SPEKOAI_MCP_BASE_URL` — public URL of this server
- `SPEKOAI_OAUTH_AUDIENCE` — optional; defaults to `${SPEKOAI_MCP_BASE_URL}/mcp` (the MCP resource URL per RFC 8707). Must also appear in the platform's `SPEKOAI_OAUTH_VALID_AUDIENCES` allowlist — otherwise Better Auth rejects the authorize request
- `SPEKOAI_BASE_URL` — optional upstream override (default `https://api.speko.ai`)

If any of the required four are set, they must all be set — otherwise
`build_auth()` returns `None` and the server runs public with a log
line. Partial-OAuth configs are rejected at startup.

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
