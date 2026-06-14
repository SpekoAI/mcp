# Pipecat to Speko Migration Guide

Use this guide when converting a Pipecat pipeline to Speko.

## Target Shape

- Treat the Pipecat pipeline as the source of orchestration intent, not as a production Speko adapter.
- Convert provider-specific STT, LLM, TTS, transport, and tool configuration into a Speko SessionConfig.
- Use Speko SDK/platform APIs for sessions, provider routing, testing, deployment, calls, and evals.

## Agent Workflow

1. Inspect the codebase for Pipecat pipeline processors, transports, frame processors, service adapters, and function/tool callbacks.
2. Identify current providers for STT, LLM, TTS, and speech-to-speech.
3. If a config file exists, read it and call `parse_external_config(format="pipecat", raw=<file contents>)`.
4. Map every custom processor or tool explicitly. Pipecat callbacks often mix business logic with transport behavior; separate them before moving to Speko.
5. Replace provider-specific session startup with Speko SDK/platform session creation.
6. Call `create_session`, then inspect calls with `list_agent_calls` and `get_call`.
7. Deploy only after user confirmation.

## Common Mapping

- Pipeline prompt/context -> `systemPrompt`
- language settings -> `intent.language`
- provider service classes -> `stackPreferences.allowedProviders`
- custom processors -> SDK-side code or Speko webhook/builtin tools
- transport setup -> Speko client/session creation

## Do Not

- Do not pretend Pipecat frame processors become Speko tools automatically.
- Do not deploy until custom processors and callbacks have an explicit mapping.
- Do not preserve provider lock-in unless the user specifically wants it.
