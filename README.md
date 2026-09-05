# spekoai-mcp

FastMCP v4 server for [SpekoAI](https://speko.ai). The hosted endpoint is:

```text
https://mcp.speko.ai/mcp
```

It accepts authenticated `POST` requests using either OAuth or a Speko
Platform API key. Configure the URL and let the client negotiate the protocol;
do not add protocol headers by hand:

```text
Authorization: Bearer <OAuth access token or sk_live_xxx>
Content-Type: application/json
```

The transport is stateless and JSON-only. Modern clients use MCP `2026-07-28`
directly; handshake-era clients such as Cursor negotiate MCP `2025-11-25`
through `initialize`. Both paths remain sessionless: there is no
`Mcp-Session-Id`, SSE response, local OAuth transaction store, or stdio
transport. Better Auth owns authorization, consent, refresh tokens, and client
registration; the MCP service only validates signed access tokens per request.

## Client setup

Use the direct-HTTP installer and select OAuth (the default interactive choice):

```bash
npx @spekoai/mcp@latest init
```

Or configure an OAuth-capable client directly. For example, Codex reads:

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

Hosted tool surfaces are bound to hostnames; a `profile` query parameter is
ignored. The primary endpoint exposes operational tools using domain/action
names:

- account: `organization.get`, `credits.balance.get`,
  `credits.ledger.list`, `usage.summary.get`;
- agents: `agents.list`, `agents.preview_stacks`, `agents.create`,
  `agents.get`, `agents.update`, `agents.delete`, deployment/version/tool and
  monitor operations;
- sessions and calls: create, list, inspect, transcript, recording, and test
  call operations;
- phone numbers, knowledge bases, evals, migration helpers, audio helpers, and
  `docs.search`.

### Builder profile

App builders use the curated host at:

```text
https://builder-mcp.speko.ai/mcp
```

It contains docs search, catalogs, agent reads, stack preview, integration code
snippets, the test-call review path, and the limited `agents.create` and
`agents.test_call` writes. Generated applications use Speko SDKs at runtime;
they do not call MCP tools.

### Assistant directory hosts

Assistant directories are published on their own hosts, each shaped by that
directory's policy. The surface is a property of the host: there is no query
parameter, header, or account setting that widens it.

#### Anthropic MCP Directory

```text
https://anthropic.speko.ai/mcp
```

**35 tools.** Reads across the account, speech-to-text, outbound calling, and
the business declaration that unlocks it:

- account: `organization.get`, `credits.balance.get`, `credits.ledger.list`,
  `usage.summary.get`;
- agents: `agents.list`, `agents.get`, `agents.preview_stacks`,
  `agents.versions.list`, `agents.tools.list`, `agents.tools.get`,
  `agents.calls.list`;
- sessions and calls: `sessions.list`, `sessions.get`,
  `sessions.transcript.get`, `sessions.recording.get`, `calls.get`,
  `calls.recording.get`;
- phone numbers: `phone_numbers.list`, `phone_numbers.get`,
  `phone_numbers.available.search`;
- knowledge bases: `knowledge_bases.list`, `knowledge_bases.get`,
  `knowledge_bases.documents.list`, `knowledge_bases.documents.get`;
- evals and monitoring: `evals.get`, `agents.monitoring.results.list`;
- audio: `audio.transcribe`;
- calling: `sessions.phone.create` — one outbound call per tool call, with
  AI disclosure injected server side;
- compliance: `phone_numbers.kyb.get`, `phone_numbers.kyb.submit` — the business
  declaration (business name, intended use, attestation) required before any
  outbound call and before buying a number;
- migration helpers: `migration.workspace.inspect`,
  `migration.external_config.parse`, `migration.session_config.build`,
  `migration.briefing.render`;
- other: `docs.search`.

Deliberately **not** on this host, because each one either produces synthetic
speech on demand or arms something that will:

| group | absent |
| ----- | ------ |
| audio generation | `audio.synthesize` |
| live session / call creation | `sessions.create`, `agents.test_call` |
| agent configuration and deployment | `agents.create`, `agents.update`, `agents.deploy`, `agents.rollback`, `agents.tools.create`, `agents.tools.update`, `agents.tools.delete` |
| knowledge-base writes | `knowledge_bases.create`, `knowledge_bases.documents.create`, `knowledge_bases.documents.delete`, `knowledge_bases.documents.finalize` |
| phone number provisioning | `phone_numbers.create`, `phone_numbers.update` |
| bulk or scheduled evaluation | `agents.evals.*`, `agents.monitors.*` |
| irreversible and outward-facing writes | `agents.delete`, `phone_numbers.delete`, `knowledge_bases.delete`, `share_cards.create` |

Configuring an agent is equivalent to arming it: a deployed agent speaks on
inbound traffic with no further tool call. Agent create, update, deploy and
rollback are therefore withheld alongside the generation tool itself.
`audio.transcribe` stays because speech-to-text produces no audio and returns
only text.

`sessions.phone.create` is on this host on purpose. It places one call, to one
number, per explicit tool call, using an agent the customer deployed elsewhere
— there is no bulk, scheduled or unattended path to it, and the caller cannot
configure the agent that speaks. Every call created on a directory host has AI
disclosure injected server side: the first thing said is that the caller is an
AI, and the agent cannot be prompted out of admitting it. A surface that can
read the transcript of a call it cannot place is a viewer, not a connector.
`sessions.create` (a browser session token, useless in a chat client) and
`agents.test_call` (two synthesized agents talking to each other) stay out.

The last row is cut for a different reason. Deleting removes capability rather
than arming anything, so those tools are not covered by the rule above — but a
published surface has to be describable in one honest clause, and three
irreversible deletes plus a creator of public pages meant this one could not be
called a read surface. `phone_numbers.delete` also releases a billed number.
Reads of all four resources stay. Calling a withheld tool on this host returns
`Unknown tool: '<name>'`, identical to a name that was never registered.

#### OpenAI Plugin Directory

```text
https://chatgpt.speko.ai/mcp
```

**18 tools.** A separate list, not a reuse of the Anthropic one, because the
two directories forbid different things: OpenAI has no restriction on generated
audio, so `audio.synthesize` and outbound calling stay, while its rules on
selling digital goods remove phone-number provisioning and the credits and
usage reads.

## Authentication and downstream calls

OAuth clients discover Better Auth from the MCP protected-resource metadata.
Better Auth issues JWT access tokens bound to the exact MCP resource URL. The
MCP server verifies their signature, issuer, audience, expiry, and scopes on
every request without retaining OAuth or protocol state.

Product-wide Platform API keys are independently verified with
`GET /v1/auth/api-key-context` on every request. Speko API tools forward API
keys unchanged. For OAuth callers, the MCP service mints a separate 60-second
JWT bound to the Platform API; it never forwards the client-presented MCP token.

Configuration:

- `SPEKOAI_API_URL` — Platform API origin, default `https://api.speko.dev`;
- `SPEKOAI_ROUTER_URL` — Speko Router origin, default `https://router.speko.dev`.
  `audio.transcribe` prefers the Router and falls back to the Platform endpoint
  when the Router cannot serve the request: it decodes WAV/PCM only, so a
  call recording in another container stays on Platform, and it authenticates
  Speko API keys only, so an OAuth-delegated session does too;
- `SPEKOAI_OAUTH_ISSUER` — Better Auth issuer, for example
  `https://platform.speko.ai/api/auth`;
- `SPEKOAI_MCP_BASE_URL` — public MCP origin used to derive the exact resource
  audience;
- `SPEKOAI_MCP_DELEGATION_SECRET` — 32+ character secret shared only with
  Platform for stateless API delegation;
- `SPEKOAI_MCP_DEFAULT_PROFILE` — deployment-only tool surface served at bare
  `/mcp` (`builder` | `connector` | `chatgpt` | `customer`). Hosted services
  set it explicitly; public query parameters never select or override it.
  Unset preserves the legacy full surface for local and self-hosted use.

See [docs/fastmcp-v4.md](docs/fastmcp-v4.md) for deployment order, smoke tests,
and failure diagnosis.
