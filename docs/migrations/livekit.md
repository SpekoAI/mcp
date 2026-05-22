# LiveKit Agents to Speko Migration Guide

Use this guide when converting a LiveKit Agents worker to Speko.

## Target Shape

- Keep LiveKit as the room/runtime when the app already depends on it.
- Route STT, LLM, and TTS through Speko via `@spekoai/adapter-livekit`.
- Keep custom LiveKit tools explicit. Do not assume a tool can be migrated automatically.
- For hosted Speko deployments, convert the worker defaults into a Speko SessionConfig and deploy through `speko_deploy`.

## Agent Workflow

1. Inspect the codebase for `@livekit/agents`, room connection code, VAD setup, STT/LLM/TTS plugins, and tool definitions.
2. Read `spekoai://docs/llms-full` and `spekoai://docs/adapter-livekit-readme`.
3. If a config file exists, run `speko_migrate(from_platform="livekit", config_path=<path>, deploy=false)`.
4. Map every unmappable tool to one of:
   - a Speko webhook tool,
   - a Speko builtin tool,
   - SDK-side tool handling that stays in the LiveKit worker.
5. Update the worker to construct Speko components with the Speko SDK/client/adapter pattern.
6. Run `speko_test` against the draft or deployed agent.
7. Deploy only after the user confirms the mapped tools and behavior.

## Common Mapping

- LiveKit instructions -> `systemPrompt`
- language/locale -> `intent.language`
- provider plugin choices -> `stackPreferences.allowedProviders`
- VAD remains LiveKit/Silero-side
- tool schemas remain explicit; tool execution should be mapped deliberately

## Do Not

- Do not leak `SPEKO_API_KEY` to the browser.
- Do not deploy while `unmappable_tools` is non-empty.
- Do not invent `/v1/sessions` fields. Read `spekoai://docs/llms-full` before editing session code.
