# Retell to Speko Migration Guide

Use this guide when converting a Retell voice agent to Speko.

## Target Shape

- Migrate through Speko SDK/platform APIs.
- Use Retell config as input for a Speko SessionConfig.
- Existing `@spekoai/adapter-retell` files are scaffold-only. They are not production-ready and must not be presented as the migration runtime.

## Agent Workflow

1. Inspect Retell agent JSON, prompt, voice, LLM, webhook, function, and call settings.
2. Read `spekoai://docs/llms-full` and `spekoai://docs/adapter-retell-readme` only as scaffold/reference material.
3. If the Retell MCP is connected, call `list_agents` and
   `list_retell_llms`, then pass those payloads to
   `speko_plan_retell_migration`.
4. For each prompt-based Retell agent selected for migration, call
   `speko_migrate_retell_agent(retell_agent=<agent>, retell_llm=<llm>, deploy=false)`.
5. Run `speko_migrate(from_platform="retell", config_path=<path>, deploy=false)`
   only when the source is a local Retell config file rather than Retell MCP output.
6. Map every Retell function/tool explicitly to Speko webhook tools, builtins, or SDK-side handlers.
7. Replace Retell API calls with Speko SDK/platform session, deploy, call, log, and eval APIs.
8. Run `speko_test`, then inspect calls with `speko_logs` and `speko_calls_get`.
9. Deploy only after user confirmation.

## Common Mapping

- Retell prompt -> `systemPrompt`
- Retell begin message -> `firstMessage`
- Retell voice -> `voice`
- language -> `intent.language`
- LLM config -> `llmOptions` and provider preferences
- default dynamic variables -> `dynamicVariables` migration metadata for review
- Retell functions -> explicit Speko tools
- Retell webhooks -> Speko webhook tools or app routes invoked by Speko

## Do Not

- Do not use `@spekoai/adapter-retell` as if it were production-ready.
- Do not deploy while Retell functions remain unmapped.
- Do not keep Retell API keys or runtime calls in the migrated production path unless the user explicitly asks for a hybrid migration.
