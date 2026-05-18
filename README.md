# spekoai-mcp

Model Context Protocol server for [SpekoAI](https://speko.ai) — the
authoritative source for SpekoAI's SDKs, adapters, and platform via
MCP. Designed so an agent (Claude Code, OpenCode, Cursor) can
authenticate once and then answer any SpekoAI question or scaffold a
new project without external lookups.

A hosted version is available at `https://mcp.speko.ai`. Clients should
install the authenticated endpoint `https://mcp.speko.ai/mcp-auth`
when they need account-scoped tools like `get_balance`. It supports
OAuth for interactive clients and Speko API keys for clients that can
send custom request headers. Use `https://mcp.speko.ai/mcp` only for
anonymous docs and scaffolding access.

## Install in an MCP client

Recommended for OAuth-capable clients:

```json
{
  "mcpServers": {
    "spekoai": {
      "url": "https://mcp.speko.ai/mcp-auth"
    }
  }
}
```

For clients that cannot complete OAuth but can send custom headers:

```json
{
  "mcpServers": {
    "spekoai": {
      "url": "https://mcp.speko.ai/mcp-auth",
      "headers": {
        "Authorization": "Bearer sk_live_xxx"
      }
    }
  }
}
```

Public-only fallback:

```json
{
  "mcpServers": {
    "spekoai": {
      "url": "https://mcp.speko.ai/mcp"
    }
  }
}
```

The authenticated endpoint is a superset of the public endpoint, so
clients normally replace the public MCP entry with `/mcp-auth` instead
of configuring both.

## Surfaces

### Resources — bundled product docs

The hosted docs exports from `docs.speko.dev/llms.txt` and
`docs.speko.dev/llms-full.txt`, public SDK/adapter READMEs, migration
guides, and the Node quickstart ship inside the wheel as MCP resources.

- `spekoai://docs/index` — start here; lists every bundled doc with a
  one-line summary.
- `spekoai://docs/{slug}` — open a specific doc. Slugs include
  `llms`, `llms-full`, `sdk-readme`, `client-readme`,
  `sdk-python-readme`, `adapter-livekit-readme`,
  `adapter-vapi-readme`, `adapter-retell-readme`,
  `mcp-server-readme`, `quickstart-node-readme`,
  `quickstart-node-index-ts`, and the `migration-*` guides.

Use `spekoai://docs/llms-full` as the primary agent reference for the
current API surface, SDK examples, and guide content generated from the
public docs site. `spekoai://docs/llms` is the compact index. READMEs are
package-level prose walkthroughs.

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
| `private_mcp_setup` | Explains how to switch from the public SpekoAI MCP endpoint to the authenticated endpoint for private account tools. Use when a user asks for balance, credits, billing, usage, organization, or other private account data from the public MCP. |
| `search_docs` | Full-text search over bundled SpekoAI docs. Returns slug + snippet + score. |
| `list_packages` | Structured manifest of every SpekoAI package with URIs to its README and `llms-full` docs resource. |
| `recommended_stack` | Opinionated SpekoAI stack for one Speko use case (`general`, `healthcare`, `finance`, `legal`). Returns packages, tagline, use-case-specific rationale and compliance warnings, and a handoff to `scaffold_voice_app`. |
| `scaffold_voice_app` | Strict Next.js App Router scaffold manifest for a browser voice app. Args: `use_case`, `languages?` (`en`/`es`, default `['en']`), `system_prompt?` (overrides the use-case default). Emits four files (route handler, React component, page, `.env.example`) plus install commands and env vars. |
| `get_balance` | Caller's current prepaid credit balance in USD (`balance_usd`, `currency`, `updated_at`). Requires `/mcp-auth`; forwards the caller's OAuth token or API key to `api.speko.dev/v1/credits/balance`. |

The public endpoint at `/mcp` exposes only the knowledge surface:
resources, prompts, `search_docs`, `list_packages`, `recommended_stack`,
and `scaffold_voice_app`. It ships static bundled data and needs no
credentials. The authenticated endpoint at `/mcp-auth` exposes that same
knowledge surface plus `get_balance`, an action tool that calls
`api.speko.dev` on the caller's behalf. See the `Auth model` section below.
The public endpoint also exposes `private_mcp_setup`, so agents can tell
users about authenticated private tools and ask whether they want to
replace/switch their public MCP connection to `/mcp-auth` when they
request account-specific actions. `/mcp-auth` includes the public tools,
so clients normally do not need to keep both endpoints configured.

## Auth model

The server has no long-lived SpekoAI credential of its own. Action
tools forward the caller's credential straight to the SpekoAI API. The
credential can be an OAuth access token minted by the platform or a
Speko API key supplied by the MCP client as `Authorization: Bearer ...`.

`get_balance` is the first such action tool. When an MCP client connects
to the hosted `/mcp-auth` endpoint, FastMCP verifies the OAuth token or
Speko API key, and the tool forwards the same bearer credential to the
SpekoAI API.
