# Changelog

## 0.1.14

- Builder tool profile: ?profile=builder serves a 12-tool preset for AI app builders (v0, Lovable, Bolt, Replit, Base44, Figma Make) incl. new voices.list, models.list, and code_snippets.get; default profile byte-identical

## Unreleased

- Builder tool profile: `/mcp?profile=builder` serves a curated 12-tool
  preset for app builders (v0, Lovable, Bolt, Replit, Base44, Figma Make) —
  reads `docs.search`, `voices.list`, `models.list`, `agents.list`,
  `agents.get`, `agents.preview_stacks`, the `agents.test_call` review
  path (`calls.get`, `sessions.transcript.get`, `calls.recording.get`),
  the new `code_snippets.get` (ready-to-paste web-voice-call +
  session-mint code for nextjs/react/node/python/curl), and writes
  limited to `agents.create` + `agents.test_call`. The default `/mcp`
  surface is unchanged; the three builder-only tools are hidden (and not
  callable) without the query param.

## 0.1.13

- Added new agents.test_call_agent tool

## 0.1.12

- Revert the `offline_access` OAuth-scope work (0.1.9–0.1.11). Advertising scopes pushed sign-in into FastMCP's consent step, which fails with `Authorization session mismatch` on multi-instance / cold-started Cloud Run: the proxy's consent cookies + transaction store use a per-process key with no shared backing store, so the state set at `/authorize` can't be verified at consent/callback when a different instance handles it. Restores the prior `OAuthProxy` config (no advertised scopes) so sign-in works without errors. Clients re-authenticate per session again — the refresh-token feature will return once the proxy has a fixed `jwt_signing_key` + a shared `client_storage` (Redis).

## 0.1.11

- Fully fix `invalid_scope: Client was not registered with scope openid` (0.1.10 was incomplete). `default_scopes` only covers a client that registers with an OMITTED scope; clients that register with an empty (`""`) or partial scope — and clients registered before `offline_access` was advertised — still failed the `/authorize` scope check. Normalize every loaded client's scope to the advertised set in `get_client`, so the advertised scopes are always grantable for new, partial, and grandfathered clients alike (no cache-clearing needed). The scope the client actually requests is still what's forwarded upstream.

## 0.1.10

- Fix `invalid_scope: Client was not registered with scope openid` on OAuth sign-in (regression from 0.1.9). `valid_scopes` only advertises/bounds scopes; it doesn't assign any at registration, so DCR clients that register without an explicit scope (e.g. Claude Code) ended up with an empty registered scope and then failed the `/authorize` scope check for the now-advertised `openid`. Set `default_scopes` so a no-scope registration is granted `openid`/`profile`/`email`/`offline_access` (matching what the client requests and what we forward upstream).

## 0.1.9

- Advertise `offline_access` (plus the standard OIDC scopes) in the OAuth metadata so MCP clients receive a refresh token — clients like Claude Code no longer re-authenticate on every restart (#740).
- `create_agent` always previews and prompts for objective/region instead of applying a silent default; add a `preview_stacks` tool (#721, #722).
- Agent creation drives the whole stack from the live selector / region (#681).
- Centralize transcript reconciliation in `@spekoai/client` and migrate consumers (#694).
- Cross-platform `uv`-guarded nx targets (Windows `cmd.exe`).

## 0.1.8

- Serve the Glama connector manifest at `/.well-known/glama.json` from the hosted MCP origin (moved out of the marketing site), so glama.ai validates against `mcp.speko.dev`.

## 0.1.6

- Inline required/optional JSON body shapes into every write-tool description (`create_session`, `create_phone_session`, `update_agent`, `create_agent_tool`, `update_agent_tool`, `create_phone_number`, `update_phone_number`, `create_knowledge_base`, `create_knowledge_document`, `create_agent_eval`, `build_session_config`), derived from the live server route validators.
- Pre-validate `create_session`, `create_phone_session`, `update_agent`, and `create_agent_tool` bodies with corrective `next_step` errors before any API call.
- Re-register the docs self-serve surface on the authenticated `/mcp` endpoint: `search_docs` tool plus `spekoai://docs/index` and `spekoai://docs/{slug}` resources.

## 0.1.5

- Don't copy over voice id

## 0.1.4

- fix: create agent payload

## 0.1.3

- Rebuild hosted MCP around the authenticated /mcp endpoint and unprefixed operational Speko API tools.

## 0.1.2

- Promote MCP server to production.

## 0.1.1

- Add Retell MCP migration planning and agent conversion tools.
- Preserve Retell prompt metadata, begin messages, voices, LLM models, dynamic variables, post-call analysis, and tool names in Speko migration drafts.

## 0.0.1

- Initial scaffold: FastMCP v3 server wrapping `spekoai.AsyncSpekoAI`.
- Tools: `create_session`, `get_session`, `end_session`, `get_usage_summary`.
- Transports: HTTP and `--stdio` for local development.
