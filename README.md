# spekoai-mcp

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

## Real phone calls from Claude Code

Three tools let an MCP client place real, disclosed phone calls on the
user's behalf:

- `check_call_readiness` - read-only "am I set up to call?" preflight (auth,
  credit, outbound caller ID, `call_me` phone).
- `lookup_business` - resolve a business name (plus optional location) to
  dialable candidates and mint a signed `dial_token` for each callable one.
- `make_call` - place the call authorized by a `dial_token`, pursue a single
  transactional objective, and stay open until the call finishes, returning
  the outcome plus the transcript.
- `call_me` - ring the account owner's own verified phone number to deliver
  a message (`notify`) or to also relay the owner's spoken reply
  (`converse`).
- `check_call_readiness` - read-only preflight that reports, in one call,
  whether the account can place calls: authentication, prepaid credit
  balance, outbound caller-ID readiness, and the `call_me` verified phone -
  each with a concrete next step. Run it first if a call does not work, or as
  the simple "am I set up?" check before the first `make_call`.

The flow is always lookup first, then dial:

```txt
lookup_business("Joe's Pizza", "New York")
  -> candidates with line types and a dial_token for each callable business
make_call(dial_token, "Do you have a table for 4 at 8pm?", "Amirlan")
  -> waits for the call, returns the OUTCOME line and the transcript
```

`make_call` does **not** require the account to own a phone number: the `from`
caller ID is optional and resolves to the deployment's server default, so a
new user can call a business without provisioning anything. `call_me` is the
only tool that needs a verified phone (the account owner's own number).
`check_call_readiness` makes both prerequisites self-serve to diagnose.

If a call outlives the client timeout, `make_call` returns the `call_id`;
use `get_call(call_id)` to check it later. If the deployment has no outbound
SIP/telephony configured, a dial returns immediately as **not placed**
(status `dialing-stub`) with a clear message instead of hanging.

### Safety rails

- **Business lines only.** Every candidate phone number goes through a
  carrier line-type check; mobile and personal lines are never dialed.
- **Hard-coded AI disclosure.** Every call opens with a mandatory
  AI-disclosure sentence that no parameter can override, and the agent
  truthfully confirms it is an AI when asked.
- **Objective screening, block-list first.** Selling, promotion, surveys,
  fundraising, and campaigning are refused before dialing, and the
  block-list always wins — a blocked intent cannot ride in on transactional
  wording. Objectives matching no blocked keyword (reservations,
  availability, pricing, order status, and other neutral questions) are
  allowed, and the in-call system prompt additionally forbids selling or
  promotion.
- **No recordings exposed.** Calling tools return outcomes and transcripts
  only; they never expose call recordings.
- **Quiet hours.** `make_call` is refused outside 08:00-21:00 destination
  local time and fails closed when the destination's UTC offset is unknown
  (`lookup_business` marks such candidates as not callable). `call_me` is
  exempt: it only ever dials the account owner's own verified number.
- **Signed dial tokens bound to the account.** `make_call` only accepts
  short-lived tokens minted by `lookup_business` and bound to the calling
  account's credential — raw phone numbers and foreign tokens are rejected.

### Self-hosting environment variables

The hosted server at `mcp.speko.ai` has these configured already. When
self-hosting, set:

| Variable | Purpose |
| --- | --- |
| `SPEKO_DIAL_TOKEN_SECRET` | Secret used to sign and verify `dial_token`s. |
| `GOOGLE_PLACES_API_KEY` | Google Places lookup behind `lookup_business`. |
| `TWILIO_LOOKUP_SID` / `TWILIO_LOOKUP_TOKEN` | Twilio carrier line-type lookup (or use `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`). |

## Auth model

The server has no long-lived SpekoAI credential of its own. Tools forward the
caller credential to the Speko API. The credential can be an OAuth access token
minted by the platform or a Speko API key supplied by the MCP client as
`Authorization: Bearer ...`.

If OAuth env vars are configured, `/mcp` accepts OAuth or Speko API keys. If
OAuth env vars are absent, `/mcp` still requires a valid Speko API key. Partial
OAuth configuration fails closed at startup.
