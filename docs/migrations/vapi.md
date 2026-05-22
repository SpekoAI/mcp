# Vapi to Speko Migration Guide

Use this guide when converting a Vapi assistant to Speko.

## Target Shape

- Migrate through Speko SDK/platform APIs.
- Convert Vapi assistant JSON into a Speko SessionConfig.
- Existing `@spekoai/adapter-vapi` files are scaffold-only. They are not production-ready and must not be presented as the migration runtime.

## Agent Workflow

1. Inspect Vapi assistant JSON, model, voice, transcriber, functions, server URLs, and call settings.
2. Read `spekoai://docs/llms-full` and `spekoai://docs/adapter-vapi-readme` only as scaffold/reference material.
3. Run `speko_migrate(from_platform="vapi", config_path=<path>, deploy=false)` when a config exists.
4. Map every Vapi function explicitly to Speko webhook tools, builtins, or SDK-side handlers.
5. Replace Vapi assistant/call creation with Speko SDK/platform session and deploy APIs.
6. Run `speko_test`, inspect calls with `speko_logs` and `speko_calls_get`, then add evals from failing calls.
7. Deploy only after user confirmation.

## Common Mapping

- Vapi `model.messages` system message -> `systemPrompt`
- Vapi transcriber -> `stackPreferences.allowedProviders.stt`
- Vapi model provider -> `stackPreferences.allowedProviders.llm`
- Vapi voice provider -> `stackPreferences.allowedProviders.tts`
- Vapi functions -> explicit Speko tools
- Vapi server URL -> Speko webhook tool endpoint or app-owned route

## Do Not

- Do not use `@spekoai/adapter-vapi` as if it were production-ready.
- Do not silently drop Vapi functions or server URL behavior.
- Do not deploy while `unmappable_tools` is non-empty.
