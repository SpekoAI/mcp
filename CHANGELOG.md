# Changelog

## 0.2.31

- A rejected action now names the fields it rejected. `/v1/actions/*` errors nest their per-field reasons under `error.fieldIssues` and their remediation under `error.nextAction`, and the parser returned as soon as it had `error.message` -- so a caller was told `INVALID_GRAPH: The graph failed structural validation.` and never which node or edge, while the reasons sat one key away in the same response. Reported 2026-09-18 by a customer who retried `agents.graph.replace` three times blind, guessing at an undeclared slot and then at an expression edge out of a tool node; the server had named the real cause each time. `agents.graph.replace`'s own description promises "the exact per-field reasons", so the schema was telling the truth about a response the client threw away.

- An agentless directory phone call now rides GPT-Live. `sessions.phone.create` with an `intent` and no `agentId` -- the connector dialing on its own -- stamps `runMode: 's2s'` on the body (`apply_directory_phone_s2s_pin`), and POST /v1/sessions/phone honours a per-call run mode from the same platform release. Until now an agentless directory call had no way to ask for the speech-to-speech route: the run mode lived only on an agent row, so the connector's own dials ran whatever cascade `intent` routing picked. On 2026-09-17 that was ElevenLabs STT, DeepSeek on Baseten and Soniox TTS for two calls to Uzbekistan whose operator expected GPT-Live. A call that names an `agentId` keeps that agent's saved run mode, a body that already says `runMode` is left alone, and an unhostable leg (no OpenAI credential, worker unavailable, self-call) still runs the cascade with `s2s.fallback` on the session timeline -- the same path a speech-native agent takes, never a 4xx.
- Reverses the 0.2.27 language guard (#2693). The directory GPT-Live pin on `agents.create` applied in every language again; `DIRECTORY_S2S_LANGUAGES` and the primary-subtag check are deleted. The operator decision is that directory traffic runs GPT-Live regardless of what `GET /v1/models` lists under `languages.s2s`; a caller who wants the cascade dials an agent saved that way.

## 0.2.30

- `agents.graph.get` and `agents.graph.replace` now describe every node kind an agent may hold. The hand-written mirror of `agentGraphSchema` listed five -- `entry`, `message`, `tool`, `transfer`, `end` -- where core has thirteen, and omitted `globalEdges` along with every per-kind field the canvas writes. A workflow authored in the dashboard with a `note`, `mcp`, `sms`, `code`, `logic_split`, `press_digit`, `extract_variable` or `subagent` node therefore came back through `agents.graph.get` describing itself as something the client was not allowed to send, and because `agents.graph.replace` is a whole-object replace, a read-edit-write round trip dropped the node or looked illegal before it was tried. The server has always accepted all thirteen; only the teaching schema was narrow.
- Authorable is not the same as walkable. `GRAPH_RUNTIME_KINDS` is the shorter list the worker executes today -- entry, message, tool, note, transfer, end -- and publishing a kind outside it is allowed and named in the response, not rejected. The schema description says so rather than leaving the caller to infer it from a refusal that never comes.
- The mirror stays hand-written, because the published manifest is dependency-free on purpose. Nothing made the copy follow the original, so tests in `apps/server`, where both are importable, now pin them together and fail when core gains a kind the mirror does not.
- The MCP client now relays Platform's `hint` -- the remediation sentence `middleware/enrich-errors.ts` writes from the curated registry in `packages/core` onto every coded error. The parser read `error`, `message`, `detail` and `code` and dropped `hint`, so the one field written specifically to tell a caller what to DO never reached an MCP user on any error, while SDK and REST callers received it normally. It is appended to the message unless the message already contains it, because the phone-authorization 403s name their URL inline and would otherwise say it twice (#2721).
- The two phone-scope refusal instructions stop naming a destination of their own and defer to the URL in the message. Platform picks that URL from the calling harness -- the Claude directory page, the ChatGPT plugin page, or the workspace phone-authorization page when it can name none -- so a fixed instruction here cannot know which applied, and "the connector's consent screen" beside a settings URL handed the model two conflicting destinations (#2721).
- Corrects a claim this changelog made in 0.2.25 and repeated since: that a scope injected into `ctx.query` by an `/oauth2/authorize` before-hook is discarded by the login redirect. It is not -- Better Auth signs `ctx.query`, not the raw inbound URL, so the edit survives. The comments in `auth.py` said the same wrong thing and now describe what the authorization server actually does for the two directory hosts whose scope lists a portal froze.

## 0.2.29

- `audio.synthesize` now carries `_meta["openai/outputTemplate"]` alongside the standard `_meta.ui.resourceUri`, both pointing at `ui://speko/audio-player.html`. ChatGPT's Apps SDK reference calls its key an alias and says the standard one is read too; the live client disagrees. On 2026-09-16 `openai-mcp/1.0.0 (Codex)` called the tool against `chatgpt.speko.ai`, answered `200`, printed its "Rendered inline audio in chat" card, and never issued the `resources/read` that rendering the widget requires -- a single request for the whole turn. The Apps SDK's own example tool sets both keys, so 0.2.29 sets both.
- A test asserts the two keys resolve to the same URI rather than merely both existing, because the failure this guards against is a widget moving and one pointer being left behind.
- The extension handshake is a separate matter and is not what this release fixes. `initialize` negotiates `2025-11-25` -- the newest entry in the SDK's `HANDSHAKE_PROTOCOL_VERSIONS` -- while `io.modelcontextprotocol/ui` only rides on `capabilities.extensions`, which the SDK strips below the `2026-07-28` era. The 2026 protocol does not use the `initialize` handshake at all, so a host still performing one can never see an advertised extension, and a host that gates rendering on that advertisement will not render regardless of this key.

## 0.2.28

- `audio.synthesize` now carries a bundled MCP App, `ui://speko/audio-player.html`, so a host that implements the extension renders a player for the audio instead of a byte count. The tool returns base64 on `structuredContent` plus a one-line acknowledgment -- `summary_only=True`, because rendering the payload as text would flood the model's context -- which meant the audio arrived and nobody could hear it. The app reads the same `structuredContent` off `ui/notifications/tool-result`.
- The endpoint serves `audio/pcm;rate=24000`: headerless samples no browser will play. The app builds the 44-byte RIFF header they are missing and hands the blob to `<audio>`; anything already in a container (mp3, wav, ogg) is passed through untouched. Verified against a real 91,200-byte synthesis -- Chrome decodes the wrapped WAV at 24 kHz and reports 1.9s, matching 91200 / (24000 x 2).
- The HTML loads nothing from the network, so the resource declares no `csp` domains and no `permissions`; those gate camera, microphone, geolocation and clipboard, none of which playback needs. A test asserts the no-external-reference property rather than the metadata, because a `<script src>` added later would pass every metadata check and still be blocked in the iframe.
- Binding is opt-in per tool through `APP_CONFIG_BY_FUNCTION`, and a host without the `io.modelcontextprotocol/ui` extension ignores `_meta.ui` and still receives the acknowledgment text, so a text-only client is unchanged. The app reaches the surfaces that serve the tool -- the default deployment and the `chatgpt` preset. The Anthropic `connector` preset withholds `audio.synthesize` under that directory's ban on AI-generated audio, so it is absent there for the same reason the tool is.

## 0.2.27

- The directory GPT-Live pin now keeps the routed cascade for a language the S2S catalog does not serve. `GET /v1/models` reports `languages.s2s` as `en fil nb`; the pin applied to every language regardless, so an agent created on the ChatGPT surface in, say, Hindi was stamped `runMode: 's2s'` and pinned to a model with no leg for it. Speech-native agents have no STT/LLM/TTS stack, so the person could not repair it from settings either -- reported 2026-09-15 as "English is good but Hindi is not working properly, I tried changing the setting but the options are limited". The language is matched on its primary subtag, case-insensitively, so `en-GB` and `nb-NO` keep the pin and a body with no `intent.language` keeps it too (Platform defaults that to English).
- The served-language set is hardcoded and dated beside the pin itself, not mirrored from the catalog: both are temporary and meant to be deleted together rather than grown into a Python copy of `packages/core/src/lib/types/api.ts`.

## 0.2.26

- Agents created through a published directory host are now speech-native on OpenAI GPT-Live: `agents.create` stamps `runMode: 's2s'` and pins `openai:gpt-live-1` before the body reaches Platform, in place of the routed STT->LLM->TTS tier the server would otherwise write into `stackPreferences` at create time. Temporary and dated in the source (2026-09-15); the pin replaces a caller-supplied s2s model rather than deferring to it, because every other realtime model in the catalog is provider-direct and a SIP participant cannot fetch an ephemeral vendor credential -- GPT-Live is the one route a phone leg can host (`services/phone-s2s.ts`, where it is already the default for a speech-native agent that pins nothing).
- Scope is narrower than "directory calls", deliberately. `agents.create` exists only on the `chatgpt` surface -- the Anthropic `connector` surface withholds it -- and `sessions.phone.create` carries no run-mode field, so a phone leg goes speech-native because the AGENT row says so. A directory call therefore reaches GPT-Live only when it dials an agent created here; a call to an agent built in the dashboard, or built before this release, keeps that agent's own run mode. Covering those needs an inline run mode on `POST /v1/sessions/phone`, which is a server change and not in this release.
- A `runtime: 'pipecat'` body is left alone: Platform rejects pipecat with `runMode: 's2s'` outright, and a pin must never turn a valid create into a 4xx. The same rule covers the case the body cannot show: `runtime` is an org-level managed setting resolved server side from a feature flag, and POST /v1/agents overwrites the body with it, so an omitted `runtime` says nothing about whether this workspace can host GPT-Live. No endpoint publishes that flag, so `agents.create` sends the pinned body and, on `400 INCOMPATIBLE_RUNTIME_MODE` alone, retries once with the pre-pin body and logs that it dropped the pin. Every other refusal reaches the caller unchanged, and the agent row returned is byte-identical to an unpinned create.

## 0.2.25

- Advertise `speko:phone` in `scopes_supported`, so a connector requests it like every other scope. It was previously stamped onto the grant by the authorization server at `/oauth2/authorize`, which cannot reach a consent screen: when the user has no session -- the normal case for a fresh connector -- Better Auth redirects to the login page built from `ctx.request.url`, the raw inbound URL, so the injected scope is discarded before the browser ever sees it. Every authorization therefore consented to ten scopes, the token came back without the marker, and `POST /v1/sessions/phone` answered `403 PHONE_NUMBER_SCOPE_REQUIRED` with advice to reconnect that could not help, because the next authorization was spelled the same way. Verified against production: the requested scope list matched this list exactly, nine entries plus `offline_access`, and recorded consents carried the same ten.
- This also restores the phone clickwrap. `postLogin.shouldRedirect` triggers the attestation-and-prices screen on the presence of this scope, so with the scope never arriving the screen had never been shown and the consent row it writes was never created -- the second of the two gates on dialing.
- Advertised only where a call can actually be placed. `oauth_resource_scopes()` withholds it from a profile whose surface serves no `sessions.phone.create` -- today the `builder` preset behind v0, Lovable, Bolt and Figma Make. Advertising it there would ask a builder to approve phone access and then route them through the attestation-and-pricing clickwrap, which `postLogin.shouldRedirect` keys off this scope, for a tool that profile does not expose. The predicate mirrors `ToolProfileMiddleware.on_list_tools` branch for branch and is asserted against it, so a profile that gains or loses the calling tool moves the scope with it.
- No new capability. Dialing stays behind both gates: this scope and the workspace phone-consent row. Existing connections keep their ten-scope grants and need one reconnect; a token refresh does not add scopes.

## 0.2.24

- Accept the per-stage vendor fields on `sessions.transcript.get`. Platform now returns `sttProvider`, `sttModel`, `ttsProvider` and `ttsModel` on every cascade turn, naming the STT and TTS vendors that actually served it alongside the LLM already reported in `provider`/`model`. The bundled action manifest pins output schemas with `additionalProperties: false`, so a manifest built before those fields existed rejected every live response with `must NOT have additional properties` and the tool returned a schema error instead of a transcript. Regenerating the manifest is the whole fix; no tool behaviour changes.

## 0.2.23

- A `replit` deployment profile, served at `https://replit.speko.ai/mcp`. Twenty-eight tools, ordered build-time first: `code_snippets.get` leads instead of arriving last, because on the `builder` preset the `agents.list` and `agents.get` output schemas are ~27.9k of a 42.3k-character `tools/list` payload and push the one tool that answers "build me a voice page" past 38k. Measured against a real Replit Agent build that answered "I'll start with browser-native voice so there's no API-key setup" and shipped a page with `webkitSpeechRecognition`, five hardcoded answers and zero calls to any voice provider. The preset also serves the knowledge-base and phone-number paths that `builder` omits, so a builder who ships a voice FAQ and then asks for a knowledge base or a number does not dead-end. Gateway ops, evals, monitors, scenarios, billing, api keys, migrations and every destructive delete stay out. Not a directory profile: `apply_directory_disclosure` overwrites `firstMessage`, and Replit publishes no disclosure rule, so a builder's own greeting is left alone.

## 0.2.22

- One out-of-credit message for every tool and host. When Platform refuses a request with `402 INSUFFICIENT_CREDITS`, the tool error now names the balance, the billing page (platform.speko.ai/settings/billing), who can act, and says not to retry, instead of `next_step=Retry the Speko MCP request` or FastMCP's bare `Error calling tool ...`. `credits.balance.get` says the workspace is out of credit and names the billing page when the balance is at or below zero. `SpekoApiError` carries `balance_usd` next to `code`, and Platform bodies relayed by the Router are recognised.

## 0.2.21

- Preserve separate invocation and downstream request identities across concurrent tools. Optional attribution receipts use a bounded background writer, retain completion time, and report dropped records without raw user agents, tool arguments, or results. Enable receipts with `ATTRIBUTION_ENABLED=true`.

## 0.2.20

- audio.transcribe now converts Google Drive and Dropbox share links to direct downloads, refuses web pages (a private Drive file answers with the one sharing step to take instead of an empty transcript), reports an empty completed transcript as no_speech, and never echoes URLs or resolved addresses in errors or logs.

## 0.2.19

- Shorter, directive `phone_numbers.kyb.{get,submit}` descriptions. The first
  live run against a new workspace worked — the call was refused, the caller
  asked for both fields and would not use the auto-generated workspace name —
  but it printed `collectFromUser`, `complianceAccess`,
  `declarationPrefillSource` and a "why I stopped rather than filing it"
  section before getting to the two questions. The descriptions carried the
  reasoning behind the rules, so the caller relayed the reasoning. They now
  carry the rules only, plus an explicit instruction not to describe the tool's
  output or the policy, and to resume the blocked task after a successful
  submit instead of recapping it.

## 0.2.18

- `audio.transcribe` sends audio to the Speko Router
  (`POST router.speko.dev/v1/stt/transcriptions`) instead of the
  first-generation Platform endpoint, and takes a `word_timestamps` option that
  returns `words: [{text, start_ms, end_ms}]` for subtitles and alignment.
- Two properties of a request keep it on the Platform endpoint, both measured
  rather than assumed: the Router decodes WAV/PCM only, so a call recording in
  another container (Ogg/Opus answers `415 unsupported_media`) stays where it
  works; and the Router authenticates Speko API keys only, so a session holding
  an OAuth delegation token stays there too. A Router refusal Platform can
  serve falls back; a provider error or timeout is raised rather than retried
  on a second backend that would bill the same audio twice.
- Word timings leave the tool in milliseconds whichever surface served them.

## 0.2.17

- `phone_numbers.kyb.submit` requires `speko:write`, not `speko:compliance`. It
  was the only action in the 72-action catalog using that scope, so no
  connector's OAuth grant carried it and the tool answered
  `403 ACTION_SCOPE_DENIED` on every surface. Reproduced against
  `chatgpt.speko.ai` with the exact scope set that connector requests
  (`speko:read speko:write speko:execute`): `kyb.get` executed, `kyb.submit`
  refused. A user who hit the declaration prompt could not get past it.

## 0.2.16

- `phone_numbers.kyb.get` now says which declaration fields the PERSON has to
  state themselves. `collectFromUser` lists them and `declarationPrefillSource`
  says where every prefilled value came from, so an auto-generated workspace
  name ("Ada's Organization") is reported as `organization_name` and stays on
  the collect list: it fills the field but nobody named that business. Only a
  value the person previously declared, or one from a verified business profile,
  comes off the list — and then to be repeated back for correction, not used
  silently.
- `phone_numbers.kyb.submit`'s description now forbids the failure this
  invites: a caller inferring a plausible business name and use case from the
  conversation, showing the attestation, collecting a "yes", and filing a record
  that says nothing about who is calling. Both fields must be the person's own
  words, and a value on `collectFromUser` that they have not stated is a reason
  to stop and ask.

## 0.2.15

- `phone_numbers.kyb.get` and `phone_numbers.kyb.submit` on every surface
  (`mcp.speko.ai`, `anthropic.speko.ai`, `chatgpt.speko.ai`, and the customer
  profile). The business declaration is two fields and an attestation, so it is
  a turn of conversation rather than a form — `kyb.get` returns the prefilled
  business name and the exact attestation text to show, `kyb.submit` records it
  against the version the person agreed to, and buying a number works
  immediately after. Anthropic surface 33 -> 35, ChatGPT 18 -> 20.
- Outbound calling now requires that declaration. `POST /v1/sessions/phone`
  refuses with `PHONE_NUMBER_KYB_REQUIRED` or `PHONE_NUMBER_KYB_SUSPENDED` and
  returns the attestation text with the refusal. Calls from the shared
  caller-ID pool dial from a Speko-owned number, which makes Speko the
  carrier's customer of record, so an undeclared caller put the consent and
  do-not-call exposure on us. `complianceAccess` had been computed for the
  dashboard and enforced nowhere.
- `phone_numbers.available.search` and `phone_numbers.list` stop telling callers
  that verification can only be done in the dashboard. It can be done in the
  conversation now, and the descriptions say so.

## 0.2.14

- The Anthropic connector surface can place a call again. `sessions.phone.create`
  is served on `anthropic.speko.ai` (33 tools, up from 32); every other name the
  MCP Directory enumerated on 2026-08-27 stays withheld. The tool creates exactly
  one outbound call per explicit tool call, against an agent the customer deployed
  through another surface, and `apply_directory_disclosure` already injects AI
  disclosure into the opening line and the system prompt on every directory
  profile, so the person who answers is told before anything else is said. A
  surface that can read the transcript of a call it cannot place is a viewer, not
  a connector. `sessions.create` (a browser session token, inert in a chat client)
  and `agents.test_call` (two synthesized agents talking to each other) stay out,
  as does everything that generates speech on demand or configures an agent.
- New `DIRECTORY_CALLING_TOOL_NAMES` records the deviation explicitly, and
  `DIRECTORY_WITHHELD_TOOL_NAMES` is what assertions about the published surface
  use. `DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES` is unchanged: it stays a verbatim
  record of what the directory team asked for.

## 0.2.13

- `phone_numbers.list` carries the same note, because it is the ONLY phone tool
  on the ChatGPT surface — that profile has neither `available.search` nor
  `create`, so a caller there asking to buy a number had nothing to read at all.
- `phone_numbers.available.search` now explains what searching does NOT get you:
  buying a number is gated on business verification (KYB) that can only be
  completed in the dashboard, and the purchase tool is absent from the Anthropic
  connector surface entirely. Callers were being told "I can't" with nowhere to
  go; the description now names the page that unblocks them.

## 0.2.12

- Fix: tool results now carry their payload in the text content block, not only
  in `structuredContent`. Hosts are not required to feed both to the model and
  several do not — claude.ai web reads text blocks only — so every read tool on
  the published connector appeared to return nothing while every call actually
  succeeded. Claude Code surfaces structured content, which is why neither
  manual testing nor the suite caught it.
- `tool_text.payload_text()` is now the single renderer for both tool families,
  the manifest-generated tools and the handwritten relays, which had drifted
  apart: `SpekoAI/mcp#9` fixed only the former.
- `audio.synthesize` keeps its summary text (`summary_only=True`); its payload
  is base64 audio. Oversized payloads truncate at 100k chars with a marker
  pointing at pagination; unserializable payloads degrade to an acknowledgment
  rather than raising.
- Adds a sweep asserting every advertised tool on the `connector`, `chatgpt` and
  `customer` profiles answers in the text block.

## 0.2.11

- Add twenty contract-backed actions to the `customer` surface, covering the two
  things that were HTTP-only: authoring an agent's execution graph, and running
  the reliability loop end to end.
  - Graph: `agents.graph.get`, `agents.graph.replace`, `agents.graph.seed`.
  - Scenario library: `scenarios.list`, `scenarios.create`, `scenarios.attach`,
    `scenarios.detach`, `scenarios.archive`, `scenarios.runs.list`.
  - Test cases: `agents.evals.update`, `agents.evals.character.set`,
    `agents.evals.delete`, `agents.evals.runs.list`, `agents.evals.generate`.
  - Analysis: `agents.evals.trends.get`, `agents.config_structure.get`.
  - Fix loop: `agents.evals.runs.suggest_fix`,
    `agents.evals.suggest_prompt_fix`, `agents.apply_prompt_fix`,
    `agents.apply_stack_fix`.
- The `connector` surface the Anthropic MCP Directory scans is unchanged at 32
  tools; every action above is `customer`-only.

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
