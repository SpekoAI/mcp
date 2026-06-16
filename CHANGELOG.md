# Changelog

## 0.2.1

- Add `check_call_readiness`: a read-only preflight that reports, in one call, whether the account can place calls — authentication and organization id, prepaid credit balance vs. the per-call minimum, outbound caller-ID readiness (owned numbers and their setup status), and the `call_me` verified phone — each with a concrete next step. Grounded only in existing endpoints (`/v1/organization`, `/v1/credits/balance`, `/v1/phone-numbers`); it issues only GET requests and never dials. This makes getting set up to call self-serve from inside the MCP client.
- Fail fast when outbound telephony is not configured: a `dialing-stub` dial response (the deployment has no SIP/caller ID, so the call is not actually placed) now returns immediately as `not_placed` with a clear message instead of polling a never-terminal session for the full wait limit. Applies to both `make_call` and `call_me`.
- Fix `call_me` `converse` mode: real Speko transcripts key each turn's speaker as `source` (`"user"`/`"agent"`), not `role`, so reply extraction previously matched nothing and always reported "no recognizable reply." Reply extraction now reads `source`.
- Clearer, non-looping guidance: `make_call` dial-time rejections caused by a missing caller ID / unconfigured telephony point at `check_call_readiness` / `list_phone_numbers` instead of looping back to `lookup_business` (which cannot fix configuration). The `call_me` "no verified phone" error now lists the actual organization keys returned and is honest that the public API has no personal-phone verify endpoint, instead of pointing at a dashboard flow that does not exist. The non-conforming "no session id on a 200" path no longer claims the call was dialed.
- Document that `make_call` needs no provisioned phone number (the `from` caller ID defaults to the deployment's server default) and that `call_me` is the only calling tool that needs a verified phone.

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
