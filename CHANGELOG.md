# Changelog

## 0.2.10

- Cut `agents.delete`, `phone_numbers.delete`, `knowledge_bases.delete` and
  `share_cards.create` from the `connector` profile, taking the Anthropic MCP
  Directory surface from 36 tools to 32. These are not "arming" tools, which is
  why the 0.2.7 cut kept them; they come off for a different reason. A directory
  listing has to be describable in one honest clause, and with three irreversible
  deletes and a public-page creator present the surface could not be called a
  read surface. `phone_numbers.delete` also releases a billed number. Reads of
  all four resources stay, as does `audio.transcribe`.

## 0.2.9

- Bind each hosted tool surface to its deployment instead of selecting it from
  a public `profile` query parameter. Query strings can no longer switch or
  widen a host's tool set.
- Add dedicated bare `/mcp` contracts for `chatgpt.speko.ai` and
  `builder-mcp.speko.ai`, alongside `anthropic.speko.ai`.
- Reject unknown non-empty `SPEKOAI_MCP_DEFAULT_PROFILE` values instead of
  silently falling back to the legacy full surface.

## 0.2.8

- Expose the complete Gateway action catalog through the customer MCP profile.

## 0.2.7

Renumbered from 0.2.6: the `mcp-server-v0.2.6` tag was already on origin,
pointing at `c3556fc3` — the commit production runs, whose package version is
`0.2.5`. Release tags are not moved, so this release takes the next free
number. Tag and package version are aligned again from here.


- Cut every tool that produces synthetic speech, or arms something that will,
  from the `connector` profile published in Anthropic's MCP Directory. The
  profile previously dropped only `audio.synthesize`, on the reading that the
  policy bans generated audio alone; the directory team refuted that on
  2026-08-27 — "in this product, configuring is arming. A deployed agent speaks
  on inbound traffic with no further tool call" — so live-call creation, agent
  create/update/deploy/rollback, agent-tool and knowledge-base writes, and
  phone-number provisioning are now excluded too. `audio.transcribe` and every
  read stay, as the directory explicitly allowed. The surface goes 61 -> 36.
- Add `SPEKOAI_MCP_DEFAULT_PROFILE`, the profile served at bare `/mcp` on a
  deployment. A query parameter was the wrong place for a policy boundary: the
  directory record listed us as `https://mcp.speko.ai/mcp` and scanned that, so
  `?profile=connector` was never applied and the review found the full surface
  behind a listing that said otherwise. A deployment default cannot be dropped
  by a URL rewrite, a CDN rule, or a retyped listing field. There is
  deliberately no `?profile=full` opt-out, and an unset variable leaves the
  default surface byte-identical for existing clients.

## 0.2.5

- Expose the read-only docs MCP at POST / for origin-normalizing discovery clients.

## 0.2.4

- Expose an unauthenticated, read-only documentation MCP at /.well-known/mcp for agent discovery.

## 0.2.3

- Restore Cursor and other MCP `2025-11-25` clients on the existing `/mcp`
  endpoint through FastMCP's stateless legacy handshake while retaining native
  MCP `2026-07-28`, POST-only transport, OAuth/API-key auth, and no session IDs.
- Record sanitized legacy-protocol acceptance telemetry for a future
  ecosystem-driven deprecation.

## 0.2.2

- Add the `chatgpt` tool profile (`/mcp?profile=chatgpt`), the 18-tool surface
  published to OpenAI's Plugin Directory. It is a separate preset from
  `connector`, not a reuse of it: `connector` is shaped by Anthropic's
  directory policy, which bans AI-generated audio and so drops
  `audio.synthesize`, while OpenAI bans selling digital goods and any checkout
  path, which removes `phone_numbers.create`, `phone_numbers.available.search`
  and every `credits.*` and `usage.*` read. It borrows two builder-only tools,
  `voices.list` and `models.list`, so a plugin that can speak in hundreds of
  voices can also say which ones exist; `code_snippets.get` stays out.
- Rename `apply_connector_disclosure` to `apply_directory_disclosure`; it now
  fires for every profile in `DIRECTORY_PROFILES`, so outbound calls created
  through either published directory surface disclose that the caller is an AI.
  Direct MCP clients on the default path are still never rewritten.
- Unify Platform and Gateway API key management (#2195).

## 0.2.1

- Fix v4 package metadata and make MCP Nx checks fail closed

## 0.2.0

- Cut over to FastMCP `4.0.0b3` and MCP `2026-07-28` with POST-only,
  JSON-only, stateless requests and no initialization or session identifiers.
- Keep OAuth without MCP-side OAuth state: Better Auth is the external
  authorization server, FastMCP validates MCP-audience JWTs independently on
  every request, and API-key verification remains available for automation.
- Replace OAuth token passthrough with 60-second, API-audience delegation JWTs
  for Platform calls. Remove the old OAuthProxy, Redis, and local credential
  directory.
- Replace Router key tools with scoped `gateway.keys.list`,
  `gateway.keys.create(name)`, and `gateway.keys.revoke(key_id)` Runtime tools.
- Remove legacy protocol handling and the stdio OAuth bridge; modern clients
  use direct HTTP OAuth discovery.

## 0.1.15

- Bring back silent token refresh (SPE-142), opt-in by env so deploying the image without infra prep changes nothing. `SPEKOAI_OAUTH_JWT_SIGNING_KEY` gives the proxy a fixed signing key for its own JWTs; `SPEKOAI_OAUTH_REDIS_URL` moves ALL OAuth state (DCR clients, transactions, auth codes, JTI mappings, upstream tokens, refresh-token metadata) from the per-instance disk store to a shared, Fernet-encrypted, `spekoai-mcp-oauth`-prefixed Redis so it survives restarts and is shared across Cloud Run instances; `SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS=true` re-applies the 0.1.9–0.1.11 scope work (advertised `offline_access` + `default_scopes` + scope-normalizing `get_client`, now also covering CIMD clients like current Claude Code) so clients receive refresh tokens. Advertising fails closed unless the signing key and Redis are configured — the 0.1.9 "Authorization session mismatch" config can't be redeployed by accident. All three unset → behavior identical to 0.1.12/0.1.13.

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
