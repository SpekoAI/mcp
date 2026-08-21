# spekoai-mcp

FastMCP v4 server for [SpekoAI](https://speko.ai). The hosted endpoint is:

```text
https://mcp.speko.ai/mcp
```

It accepts authenticated `POST` requests using either OAuth or a Speko
Platform API key:

```text
Authorization: Bearer <OAuth access token or sk_live_xxx>
MCP-Protocol-Version: 2026-07-28
Content-Type: application/json
```

The transport is stateless and JSON-only. There is no initialization request,
session id, SSE response, local OAuth transaction store, or stdio transport.
Better Auth owns authorization, consent, refresh tokens, and client
registration; the MCP service only validates signed access tokens per request.

## Client setup

Use the direct-HTTP installer and select OAuth (the default interactive choice):

```bash
npx @spekoai/mcp@latest init
```

Or configure a modern OAuth-capable client directly. For example, Codex reads:

```toml
[mcp_servers.speko]
url = "https://mcp.speko.ai/mcp"
```

For automation, export a Platform API key and add the bearer setting:

```toml
[mcp_servers.speko]
url = "https://mcp.speko.ai/mcp"
bearer_token_env_var = "SPEKO_API_KEY"
```

## Tool surfaces

The default profile exposes operational tools using domain/action names:

- account: `organization.get`, `credits.balance.get`,
  `credits.ledger.list`, `usage.summary.get`;
- agents: `agents.list`, `agents.preview_stacks`, `agents.create`,
  `agents.get`, `agents.update`, `agents.delete`, deployment/version/tool and
  monitor operations;
- sessions and calls: create, list, inspect, transcript, recording, and test
  call operations;
- phone numbers, knowledge bases, evals, migration helpers, audio helpers, and
  `docs.search`;
- Gateway keys: `gateway.keys.list`, `gateway.keys.create(name)`, and
  `gateway.keys.revoke(key_id)`.

Gateway key tools require the authenticating Platform API key to carry
`gateway.keys.manage`. An organization owner or admin grants it by selecting
**Manage Gateway API keys** during key creation. Gateway secrets are returned
only once, and routing remains a per-request choice rather than key policy.

### Builder profile

App builders can request the curated profile at:

```text
https://mcp.speko.ai/mcp?profile=builder
```

It contains docs search, catalogs, agent reads, stack preview, integration code
snippets, the test-call review path, and the limited `agents.create` and
`agents.test_call` writes. Generated applications use Speko SDKs at runtime;
they do not call MCP tools.

## Authentication and downstream calls

OAuth clients discover Better Auth from the MCP protected-resource metadata.
Better Auth issues JWT access tokens bound to the exact MCP resource URL. The
MCP server verifies their signature, issuer, audience, expiry, and scopes on
every request without retaining OAuth or protocol state.

Platform API keys are independently verified with
`GET /v1/auth/api-key-context` on every request. Speko API tools forward API
keys unchanged. For OAuth callers, the MCP service mints a separate 60-second
JWT bound to the Platform API; it never forwards the client-presented MCP token.
Gateway key tools still require a Platform API key carrying
`gateway.keys.manage`, and Runtime verifies that scope again.

Configuration:

- `SPEKOAI_API_URL` — Platform API origin, default `https://api.speko.dev`;
- `SPEKOAI_GATEWAY_URL` — Runtime Gateway origin, default
  `https://gateway.speko.dev`;
- `SPEKOAI_OAUTH_ISSUER` — Better Auth issuer, for example
  `https://platform.speko.ai/api/auth`;
- `SPEKOAI_MCP_BASE_URL` — public MCP origin used to derive the exact resource
  audience;
- `SPEKOAI_MCP_DELEGATION_SECRET` — 32+ character secret shared only with
  Platform for stateless API delegation.

See [docs/fastmcp-v4.md](docs/fastmcp-v4.md) for deployment order, smoke tests,
and failure diagnosis.
