# Changelog

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
