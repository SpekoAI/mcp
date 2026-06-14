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
surface: the `search_docs` tool (full-text search over bundled Speko docs)
and the `spekoai://docs/index` + `spekoai://docs/{slug}` resources. MCP
prompts, components, and scaffolding tools are not advertised.

Tool names are unprefixed because MCP clients may already namespace tools by
server name.

### Account

- `get_organization`
- `get_credit_balance`
- `list_credit_ledger`
- `get_usage_summary`

### Agents and Tools

- `list_agents`
- `create_agent`
- `get_agent`
- `update_agent`
- `delete_agent`
- `list_agent_tools`
- `create_agent_tool`
- `get_agent_tool`
- `update_agent_tool`
- `delete_agent_tool`

### Versions, Sessions, and Calls

- `deploy_agent`
- `rollback_agent`
- `list_agent_versions`
- `create_session`
- `create_phone_session`
- `list_sessions`
- `get_session`
- `get_session_transcript`
- `get_session_recording`
- `list_agent_calls`
- `get_call`
- `get_call_recording`

### Phone Numbers, Knowledge Bases, and Evals

- `list_phone_numbers`
- `search_available_phone_numbers`
- `create_phone_number`
- `get_phone_number`
- `update_phone_number`
- `delete_phone_number`
- `create_knowledge_base`
- `list_knowledge_bases`
- `get_knowledge_base`
- `delete_knowledge_base`
- `list_knowledge_documents`
- `create_knowledge_document`
- `get_knowledge_document`
- `delete_knowledge_document`
- `finalize_knowledge_document`
- `list_agent_evals`
- `create_agent_eval`
- `run_agent_eval`
- `get_eval`

### Build and Migration Helpers

- `inspect_workspace`
- `build_session_config`
- `parse_external_config`
- `render_briefing`
- `create_share_card`

### Docs

- `search_docs` - full-text search over the bundled Speko docs (SDK/adapter
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
