# Changelog

## 0.2.0

- Add real outbound calling tools: `lookup_business` resolves a business (Google Places + Twilio line-type check) and mints signed, account-bound dial tokens; `make_call` places a disclosed, objective-scoped phone call and waits for the outcome plus transcript; `call_me` rings the account owner's verified number in `notify` or `converse` mode.
- Safety rails on every call: business lines only, a hard-coded AI-disclosure opening that no parameter can override, a block-first objective screen (selling, promotion, surveys, fundraising, and campaigning are refused; the block-list wins over transactional wording), destination quiet hours for `make_call` (08:00-21:00 local, failing closed when the destination UTC offset is unknown; `call_me` only dials the owner's own verified number), per-call nonce-delimited prompt blocks so user-supplied text cannot forge prompt structure, no recordings exposed, and dial tokens signed with `SPEKO_DIAL_TOKEN_SECRET` and bound to the caller's credential.
- Long-running call support: progress reporting while the call is live, and a `timeout` result pointing at `get_call(call_id)` when a call outlives the client wait limit.
- Resilient failure handling: a failed status poll after dialing returns the `call_id` and points at `get_call` instead of advising a retry that would re-dial, dial-time API errors carry `make_call`/`call_me`-specific guidance, and a single failed carrier lookup only marks that candidate as not callable instead of aborting the whole `lookup_business` result set.
- Document the calling flow and self-hosting env vars (`SPEKO_DIAL_TOKEN_SECRET`, `GOOGLE_PLACES_API_KEY`, `TWILIO_LOOKUP_SID`/`TWILIO_LOOKUP_TOKEN` or `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`) in the README and server instructions.

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
