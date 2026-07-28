# spekoai-mcp

[![smithery badge](https://smithery.ai/badge/abat/speko)](https://smithery.ai/servers/abat/speko)

Model Context Protocol server for [SpekoAI](https://speko.ai). The hosted
server exposes one authenticated endpoint:

```txt
https://mcp.speko.ai/mcp
```

It supports OAuth for interactive MCP clients and Speko API keys for clients
that can send custom request headers.

## Install in an MCP client

OAuth-capable clients:

```json
{
  "mcpServers": {
    "spekoai": {
      "url": "https://mcp.speko.ai/mcp"
    }
  }
}
```

API-key clients:

```json
{
  "mcpServers": {
    "spekoai": {
      "url": "https://mcp.speko.ai/mcp",
      "headers": {
        "Authorization": "Bearer sk_live_xxx"
      }
    }
  }
}
```

## Surfaces

The hosted server exposes the operational tools below plus a docs self-serve
surface: the `docs.search` tool (full-text search over bundled Speko docs)
and the `spekoai://docs/index` + `spekoai://docs/{slug}` resources. MCP
prompts, components, and scaffolding tools are not advertised.

Tool names use domain/action dot notation for client grouping.

### Builder profile

App builders (v0, Lovable, Bolt, Replit, Base44, Figma Make) can add the
server with a curated, right-sized preset instead of the full operational
surface:

```txt
https://mcp.speko.ai/mcp?profile=builder
```

Auth is identical to `/mcp` (same OAuth flow, same API-key header). The
builder profile advertises exactly these tools:

- `docs.search` — bundled Speko docs search
- `voices.list` — TTS voice + provider catalog
- `models.list` — STT/LLM/TTS/S2S provider+model catalog (`allowedProviders` ids)
- `agents.list` / `agents.get` — read agent configs
- `agents.preview_stacks` — the stack preview `agents.create` requires first
- `calls.get` / `sessions.transcript.get` / `calls.recording.get` — the
  `agents.test_call` review path (poll the call, read transcript/recording)
- `code_snippets.get` — ready-to-paste integration code (web voice call +
  server-side session mint) for `nextjs`, `react`, `node`, `python`, or `curl`
- `agents.create` / `agents.test_call` — the only writes

`voices.list`, `models.list`, and `code_snippets.get` exist only in the
builder profile. Any other `profile` value (or none) serves the default
surface below, unchanged. Note that MCP tools only inform the builder's
agent during code generation — the generated app cannot call MCP tools at
runtime. Runtime integration is a `SPEKO_API_KEY` environment variable
plus the SDKs, which is exactly what `code_snippets.get` returns.

### Account

- `organization.get`
- `credits.balance.get`
- `credits.ledger.list`
- `usage.summary.get`

### Agents and Tools

- `agents.list`
- `agents.create`
- `agents.get`
- `agents.update`
- `agents.delete`
- `agents.tools.list`
- `agents.tools.create`
- `agents.tools.get`
- `agents.tools.update`
- `agents.tools.delete`

### Versions, Sessions, and Calls

- `agents.deploy`
- `agents.rollback`
- `agents.versions.list`
- `sessions.create`
- `sessions.phone.create`
- `sessions.list`
- `sessions.get`
- `sessions.transcript.get`
- `sessions.recording.get`
- `agents.calls.list`
- `calls.get`
- `calls.recording.get`

### Phone Numbers, Knowledge Bases, and Evals

- `phone_numbers.list`
- `phone_numbers.available.search`
- `phone_numbers.create`
- `phone_numbers.get`
- `phone_numbers.update`
- `phone_numbers.delete`
- `knowledge_bases.create`
- `knowledge_bases.list`
- `knowledge_bases.get`
- `knowledge_bases.delete`
- `knowledge_bases.documents.list`
- `knowledge_bases.documents.create`
- `knowledge_bases.documents.get`
- `knowledge_bases.documents.delete`
- `knowledge_bases.documents.finalize`
- `agents.evals.list`
- `agents.evals.create`
- `agents.evals.run`
- `evals.get`

### Build and Migration Helpers

- `migration.workspace.inspect`
- `migration.session_config.build`
- `migration.external_config.parse`
- `migration.briefing.render`
- `share_cards.create`

### Router keys

Keys for the OpenAI-compatible router at `https://api.speko.ai/v1`. A router
key carries its own routing policy — language, use case, objective, max
price, and an ordered chain per stage whose element 0 is the pin and whose
remaining elements are the failover order — so a caller who configured it
sends no routing headers.

- `router.keys.list`
- `router.keys.create`
- `router.keys.update`
- `router.keys.revoke`

These four require OAuth. A Speko API key is refused, and is never sent to
the control plane: a machine credential must not be able to mint further
credentials.

### Docs

- `docs.search` - full-text search over the bundled Speko docs (SDK/adapter
  READMEs, hosted llms.txt exports, migration guides). Hits link to
  `spekoai://docs/{slug}` resources; `spekoai://docs/index` lists every doc.

## Auth model

The server has no long-lived SpekoAI credential of its own. Tools forward the
caller credential to the Speko API. The credential can be an OAuth access token
minted by the platform or a Speko API key supplied by the MCP client as
`Authorization: Bearer ...`.

If OAuth env vars are configured, `/mcp` accepts OAuth or Speko API keys. If
OAuth env vars are absent, `/mcp` still requires a valid Speko API key. Partial
OAuth configuration fails closed at startup.

The one exception is `router.keys.*`, which requires OAuth. Router key tools
talk to the router control plane (`SPEKOAI_ROUTER_CONTROL_URL`, default
`https://control.speko.ai`) rather than the Speko API, and that plane
authenticates a signed-in user, never a machine principal.
