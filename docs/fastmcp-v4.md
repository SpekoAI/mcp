# FastMCP v4 stateless deployment

This is the internal operator guide for MCP `2026-07-28`. It is intentionally
outside the public document manifest assembled by `scripts/sync_docs.py`.

## Architecture

The hosted endpoint supports both interactive OAuth and Platform API keys
without storing protocol or OAuth state in the MCP service:

```text
MCP client -- MCP-audience OAuth JWT --> FastMCP resource server
FastMCP   -- 60-second API JWT ------> Platform API

Automation -- sk_* ------------------> FastMCP
FastMCP    -- same sk_* --------------> Platform / Runtime
```

Better Auth is the external OAuth 2.1 authorization server. It owns client
registration, consent, authorization codes, refresh tokens, and revocation.
FastMCP's `RemoteAuthProvider` publishes RFC 9728 protected-resource metadata
and validates every JWT independently against Better Auth's JWKS. There is no
`OAuthProxy`, Redis, local client store, token cache, or replica affinity.

The MCP bearer is bound to `https://mcp.speko.ai/mcp` and is never passed to an
upstream API. For ordinary tool calls, MCP signs a separate API-audience token
that expires after 60 seconds. Platform validates it and resolves the user's
organization. This is stateless: the only shared material is a signing secret,
not request or session data.

## Prerequisites

- Better Auth's OAuth provider serves OAuth/OIDC metadata, authorization,
  token, refresh, JWKS, and registration endpoints.
- Better Auth allows the exact MCP resource URL in `validAudiences`. Platform
  adds `${SPEKOAI_MCP_BASE_URL}/mcp` automatically.
- Better Auth advertises PKCE S256 and includes `iss` in authorization
  responses. Its existing DCR endpoint remains a deprecated compatibility path;
  pre-registration is preferred until Platform adopts Better Auth's CIMD
  extension.
- Platform and MCP share the same 32+ character
  `SPEKOAI_MCP_DELEGATION_SECRET`.
- Platform serves `GET /v1/auth/api-key-context` for direct API-key callers.
- Runtime has applied `000025_platform_api_key_management.sql`, requires
  `SPEKO_PLATFORM_API_KEY_CONTEXT_URL`, and accepts scoped Platform bearers on
  `/api/keys`.
- MCP dependencies are locked to `fastmcp==4.0.0b3` and
  `fastmcp-slim==4.0.0b3`.

## Configuration

MCP service:

```text
SPEKOAI_OAUTH_ISSUER=https://platform.speko.ai/api/auth
SPEKOAI_MCP_BASE_URL=https://mcp.speko.ai
SPEKOAI_MCP_DELEGATION_SECRET=<32+ character shared secret>
SPEKOAI_MCP_DELEGATION_ISSUER=https://mcp.speko.ai # optional default
SPEKOAI_API_AUDIENCE=https://api.speko.dev         # optional default
SPEKOAI_API_URL=https://api.speko.dev              # optional default
```

Platform service:

```text
SPEKOAI_MCP_BASE_URL=https://mcp.speko.ai
SPEKOAI_MCP_DELEGATION_SECRET=<same shared secret>
SPEKOAI_MCP_DELEGATION_ISSUER=https://mcp.speko.ai # optional default
SPEKOAI_API_AUDIENCE=https://api.speko.dev         # optional default
```

Remove the retired MCP proxy settings:

```text
SPEKOAI_OAUTH_CLIENT_ID
SPEKOAI_OAUTH_CLIENT_SECRET
SPEKOAI_OAUTH_JWT_SIGNING_KEY
SPEKOAI_OAUTH_REDIS_URL
SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS
FASTMCP_HOME
```

`SPEKOAI_OAUTH_ISSUER` now means the actual authorization-server issuer
(`.../api/auth`), not the old proxy's operational prefix (`.../api/auth/oauth2`).

## Protocol contract

- `/mcp` accepts authenticated JSON `POST` requests only.
- Every request carries exactly one `MCP-Protocol-Version: 2026-07-28` header.
- `initialize`, legacy protocol versions, `Mcp-Session-Id`, GET/DELETE
  transport requests, SSE, and stdio are unsupported.
- OAuth discovery routes are public GETs outside `/mcp`.
- FastMCP serves `/.well-known/oauth-protected-resource/mcp`; authorization,
  token, refresh, and registration routes remain on Better Auth.
- Protected-resource scopes are `openid profile email`. `offline_access` is
  advertised by the authorization server, not as a resource requirement.

## Deployment order

1. Deploy Platform with the MCP resource audience, delegation verification,
   and API-key context endpoint.
2. Apply the Runtime migration and deploy scoped API-key support.
3. Configure the shared delegation secret on Platform and MCP.
4. Deploy FastMCP v4, the direct-HTTP installer, and public docs.
5. Remove obsolete OAuthProxy/Redis/client-credential settings.

## Smoke tests

Verify discovery first:

```bash
curl -sS https://mcp.speko.ai/.well-known/oauth-protected-resource/mcp
curl -sS https://platform.speko.ai/.well-known/oauth-authorization-server/api/auth
```

The protected-resource document must identify
`https://mcp.speko.ai/mcp`, list the Better Auth issuer, and omit
`offline_access`. Complete a browser OAuth connection in a modern MCP client,
then call `server/discover` and `organization.get` twice with requests routed to
different replicas. Responses must contain no `Mcp-Session-Id`.

For API-key coverage, set `MCP_URL` and `SPEKO_API_KEY`:

```bash
curl -sS "$MCP_URL" \
  -H "Authorization: Bearer $SPEKO_API_KEY" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Content-Type: application/json" \
  --data '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"io.modelcontextprotocol/clientInfo":{"name":"smoke","version":"1"},"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}'
```

## Failure diagnosis

| Symptom | Check |
| --- | --- |
| OAuth discovery returns 404 | `SPEKOAI_OAUTH_ISSUER` is set and ends in `/api/auth`; the MCP well-known route is mounted outside the protocol guard. |
| OAuth bearer returns 401 | JWT `iss` equals Better Auth, `aud` equals the exact MCP URL, and JWKS is reachable. |
| Tool authenticates but Platform returns 401 | The same delegation secret/issuer/API audience is configured on MCP and Platform. |
| Browser sign-in fails at registration | Pre-register the client or enable Better Auth's DCR compatibility endpoint; CIMD needs the future Better Auth extension rollout. |
| API-key bearer returns 401 | Key starts with `sk_`, is not revoked, and Platform context returns 200. |
| Protocol 400 / -32020 | Send exactly one version header and no session id. |
| Protocol 400 / -32022 | Upgrade the client to MCP `2026-07-28`. |
| Different replicas disagree | Verify no old OAuthProxy image remains and no request depends on local state. |
