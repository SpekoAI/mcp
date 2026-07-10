# Vapi to Speko Migration Guide

Use this guide when converting a Vapi assistant to Speko.

## Target Shape

- Migrate through Speko SDK/platform APIs.
- Convert Vapi assistant JSON into a Speko SessionConfig.
- Existing `@spekoai/adapter-vapi` files are scaffold-only. They are not production-ready and must not be presented as the migration runtime.

## Agent Workflow

1. Inspect the raw Vapi assistant JSON, model, voice, transcriber, tools, server URLs, and call settings.
2. Read `spekoai://docs/migration-vapi` or call `docs.search("vapi migration")`.
3. Call `migration.external_config.parse(format="vapi", raw=<file contents>)`. Its output is a scaffold: verify every field against the raw Vapi JSON and inspect `unmappable_tools`.
4. Map every Vapi tool explicitly to a Speko agent tool, builtin, webhook, or SDK-side handler.
5. Use `agents.create`, `agents.tools.create`, and `sessions.create`; inspect calls with `agents.calls.list` and `calls.get`.
6. Add evals from failing calls, then call `agents.deploy` only after user confirmation.

## Common Mapping

The parser output is never authoritative. Verify every mapped value and omission against the raw Vapi JSON.

| Vapi field | Speko field or required handling |
| --- | --- |
| `model.messages[role=system]` | `systemPrompt` |
| `model.provider` | `stackPreferences.allowedProviders.llm` |
| `model.model`, `model.temperature` | `llmOptions` |
| `voice.provider: "11labs"` | MUST pin `stackPreferences.allowedProviders.tts` to `["elevenlabs"]`; otherwise routing may select another TTS and the `voiceId` fails. |
| `voice.voiceId` | `voice`, passed through verbatim |
| `transcriber.provider`, `transcriber.model` | `stackPreferences.allowedProviders.stt` |
| `transcriber.language` | `intent.language` |
| `transcriber.keywords` | `sttOptions.keywords` |
| `firstMessage` | `firstMessage`; for `assistant-waits-for-user`, use `""` on `agents.create` (`null` is rejected there) and `null` on `sessions.create` or agent PATCH. |
| `model.tools`, `toolIds`, legacy functions | Explicit agent tools via `POST /v1/agents/:id/tools`; never silently drop them. |
| `serverUrl`, `serverMessages` | Agent webhooks: `preCall`, `postCall`, `status`, `analysis`, or `recording`. These are lifecycle semantics, not a message stream. |
| `variableValues` | Session variables; `{{var}}` syntax is unchanged. |
| `startSpeakingPlan`, `stopSpeakingPlan` | `turnHandling` endpointing and interruption settings |
| `analysisPlan` | `webhooks.postCall.extractionFields` |
| `backgroundSound` | `backgroundAudio.ambient`; only three built-in clips are available. |

## No Speko Equivalent

Disclose any used feature to the user: `say()`, volume-level events, assistant-level `voicemailDetection` (Speko supports it only on warm transfers), hooks, squads/workflows, `endCallPhrases`, and ElevenLabs fine-tune parameters such as `stability` or `similarityBoost`.

## Browser apps (Vapi Web SDK)

Speko has no browser public key. Add one backend route that calls `POST /v1/sessions` server-side with `Authorization: Bearer $SPEKO_API_KEY`, then returns `transportToken` and `transportUrl`. The browser creates the call with `@spekoai/client`'s `VoiceConversation.create`.

Event mapping: `call-start` -> `onConnect`; `call-end` -> `onDisconnect`; `speech-start`/`speech-end` -> `onModeChange`; `message` -> `onTranscript`; `error` -> `onError`.

Method mapping: `vapi.send` add-message -> `sendChatMessage`; `setMuted` -> `setMicMuted`; `stop` -> `endSession`.

See the [realtime conversation guide](https://docs.speko.dev/guides/realtime-conversation) and the [Vapi migration skill](https://speko.ai/skills/speko-migrate-vapi/SKILL.md).

## Do Not

- Do not use `@spekoai/adapter-vapi` as if it were production-ready; it is scaffold-only.
- Do not silently drop Vapi functions or server URL behavior.
- Do not deploy while `unmappable_tools` is non-empty.
- Do not deploy without explicit user confirmation.
