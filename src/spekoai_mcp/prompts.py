"""MCP prompts for scaffolding and migration workflows.

Claude Code and similar clients surface MCP prompts as slash commands.
These prompts return explicit resource URIs for the agent to read next
— the heavy lifting is in `spekoai://docs/*`, not hidden in the prompt.

Unsupported language+scenario combos (e.g. `voice_conversation` in
Python) raise a clear error that tells the agent which combo to pick
instead, rather than silently returning a broken scaffold.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import PromptError
from fastmcp.prompts.prompt import Message

Scenario = Literal[
    "voice_conversation",
    "batch_transcribe",
    "livekit_agent",
    "quickstart",
]
Language = Literal["typescript", "python"]
Runtime = Literal["bun", "node", "deno"]
MigrationRuntime = Literal["node", "bun", "deno", "python"]


_TS_ONLY_SCENARIOS: set[Scenario] = {"voice_conversation", "livekit_agent"}


def _install_cmd(runtime: Runtime, packages: list[str]) -> str:
    joined = " ".join(packages)
    if runtime == "bun":
        return f"bun add {joined}"
    if runtime == "deno":
        return f"deno add {' '.join(f'npm:{p}' for p in packages)}"
    return f"pnpm add {joined}"


def _voice_conversation_messages(runtime: Runtime) -> list[Message]:
    install = _install_cmd(runtime, ["@spekoai/client"])
    return [
        Message(
            "Scaffold a browser voice-conversation app using SpekoAI.\n"
            "Before writing code, READ these resources in full (don't "
            "paraphrase — read them verbatim; they contain the exact API "
            "shapes):\n"
            "- `spekoai://docs/llms-full` — current docs.speko.dev SDK, "
            "API, and guide export, including the exact `POST /v1/sessions` "
            "request/response shape.\n"
            "- `spekoai://docs/client-readme` — full browser SDK reference.\n\n"
            "Do NOT invent fields on `/v1/sessions` (no `agent`/`agentId`, "
            "no flat `language` — `intent` is a nested object). "
            "If any field you want to set isn't in the hosted docs, call "
            "`docs.search('<field>')` first; don't guess."
        ),
        Message(
            "Architecture:\n"
            "1. Backend exposes `POST /api/speko-session`. It calls "
            "`POST https://api.speko.dev/v1/sessions` with your "
            "`SPEKO_API_KEY` as the bearer, and returns "
            "`{ transportToken, transportUrl }` to the browser.\n"
            "2. Browser calls `VoiceConversation.create({ "
            "transportToken, transportUrl, ... })`.\n"
            "3. Never call `/v1/sessions` from the browser — that would "
            "leak `SPEKO_API_KEY`.\n\n"
            "`@spekoai/sdk` does NOT expose a sessions helper today, so "
            "the backend uses `fetch` directly. The SDK dep isn't needed "
            "unless you're also calling transcribe/complete/synthesize "
            "server-side.\n\n"
            f"Install — browser: `{install}`\n"
            "Backend deps: none beyond your runtime's `fetch`."
        ),
        Message(
            "UI expectations: the demo is an exploration surface for the "
            "session API, NOT a single Start button. Render a config "
            "panel ABOVE the Start/End/Mute controls with a form input "
            "for every `/v1/sessions` field the user might want to tune. "
            "Defaults should produce a working session so the user can "
            "hit Start without filling anything in; every field should "
            "also be editable.\n\n"
            "Form inputs (label → control → default):\n"
            "- `intent.language` → text input → `en-US` (BCP-47)\n"
            "- `intent.region` → text input → empty "
            "(server defaults to `global`; set e.g. `us-east4` to "
            "rank streaming providers in a specific region)\n"
            "- `intent.optimizeFor` → select → `balanced` "
            "(options: `balanced`, `accuracy`, `latency`, `cost`)\n"
            "- `systemPrompt` → textarea → "
            "`'You are a concise voice assistant.'`\n"
            "- `voice` → text input → empty (provider default)\n"
            "- `llm.temperature` → number input (step 0.1, min 0, max 2) "
            "→ `0.7`\n"
            "- `llm.maxTokens` → number input (min 1) → `400`\n"
            "- `ttsOptions.speed` → number input (step 0.05, min 0.5, "
            "max 2) → `1.0`\n"
            "- `ttsOptions.sampleRate` → number input → `24000`\n"
            "- `constraints.allowedProviders.stt` / `.llm` / `.tts` → "
            "three text inputs, comma-separated strings → empty\n"
            "- `identity` → text input → empty (server generates uuid)\n"
            "- `ttlSeconds` → number input (1-86400) → `900`\n"
            "- `metadata` → textarea (JSON) → `{}`\n\n"
            "Wrap advanced fields (constraints, sampleRate, ttlSeconds, "
            "metadata, identity) in a collapsed <details> so the "
            "default view isn't overwhelming. Mark `intent.language` as "
            "required. On Start, read the form values, drop empty optional "
            "fields, POST to `/api/speko-session`, then call "
            "`VoiceConversation.create`."
        ),
        Message(
            "Starter — backend session route (`server.ts`):\n\n"
            "```ts\n"
            "// POST /api/speko-session — browser sends the full config\n"
            "// body; we forward to api.speko.dev/v1/sessions as-is with\n"
            "// the server's API key. Do minimal validation — the Speko\n"
            "// API returns 400 on bad shapes, pass that error through.\n"
            "const SPEKO_API_KEY = process.env.SPEKO_API_KEY!;\n"
            "const SPEKO_BASE_URL = process.env.SPEKO_BASE_URL ?? 'https://api.speko.dev';\n\n"
            "async function createSession(config: Record<string, unknown>) {\n"
            "  const res = await fetch(`${SPEKO_BASE_URL}/v1/sessions`, {\n"
            "    method: 'POST',\n"
            "    headers: {\n"
            "      Authorization: `Bearer ${SPEKO_API_KEY}`,\n"
            "      'Content-Type': 'application/json',\n"
            "    },\n"
            "    body: JSON.stringify(config),\n"
            "  });\n"
            "  if (!res.ok) {\n"
            "    throw new Error(\n"
            "      `speko /v1/sessions ${res.status}: ${await res.text()}`,\n"
            "    );\n"
            "  }\n"
            "  const { transportToken, transportUrl } = await res.json();\n"
            "  return { transportToken, transportUrl };\n"
            "}\n"
            "```\n"
        ),
        Message(
            "Starter — browser form → session body (`src/voice.ts`):\n\n"
            "```ts\n"
            "import { VoiceConversation } from '@spekoai/client';\n\n"
            "// Read the config-panel form and drop empty optional fields\n"
            "// so Speko uses its defaults. Required: intent.language.\n"
            "// Everything else is pruned if blank.\n"
            "function buildSessionBody(form: HTMLFormElement) {\n"
            "  const data = new FormData(form);\n"
            "  const trim = (k: string) => String(data.get(k) ?? '').trim();\n"
            "  const num = (k: string) => {\n"
            "    const v = trim(k);\n"
            "    return v === '' ? undefined : Number(v);\n"
            "  };\n"
            "  const list = (k: string) => {\n"
            "    const v = trim(k);\n"
            "    return v === '' ? undefined : v.split(',').map((s) => s.trim()).filter(Boolean);\n"
            "  };\n\n"
            "  const body: Record<string, unknown> = {\n"
            "    intent: {\n"
            "      language: trim('intent.language') || 'en-US',\n"
            "      ...(trim('intent.region') && {\n"
            "        region: trim('intent.region'),\n"
            "      }),\n"
            "      ...(trim('intent.optimizeFor') && {\n"
            "        optimizeFor: trim('intent.optimizeFor'),\n"
            "      }),\n"
            "    },\n"
            "  };\n"
            "  const prompt = trim('systemPrompt');\n"
            "  if (prompt) body.systemPrompt = prompt;\n"
            "  if (trim('voice')) body.voice = trim('voice');\n"
            "  const llm: Record<string, number> = {};\n"
            "  const temp = num('llm.temperature');\n"
            "  if (temp !== undefined) llm.temperature = temp;\n"
            "  const maxT = num('llm.maxTokens');\n"
            "  if (maxT !== undefined) llm.maxTokens = maxT;\n"
            "  if (Object.keys(llm).length) body.llm = llm;\n\n"
            "  const tts: Record<string, number> = {};\n"
            "  const speed = num('ttsOptions.speed');\n"
            "  if (speed !== undefined) tts.speed = speed;\n"
            "  const rate = num('ttsOptions.sampleRate');\n"
            "  if (rate !== undefined) tts.sampleRate = rate;\n"
            "  if (Object.keys(tts).length) body.ttsOptions = tts;\n\n"
            "  const allowed: Record<string, string[]> = {};\n"
            "  const sttList = list('constraints.allowedProviders.stt');\n"
            "  if (sttList) allowed.stt = sttList;\n"
            "  const llmList = list('constraints.allowedProviders.llm');\n"
            "  if (llmList) allowed.llm = llmList;\n"
            "  const ttsList = list('constraints.allowedProviders.tts');\n"
            "  if (ttsList) allowed.tts = ttsList;\n"
            "  if (Object.keys(allowed).length) {\n"
            "    body.constraints = { allowedProviders: allowed };\n"
            "  }\n"
            "  if (trim('identity')) body.identity = trim('identity');\n"
            "  if (num('ttlSeconds') !== undefined) body.ttlSeconds = num('ttlSeconds')!;\n"
            "  const meta = trim('metadata');\n"
            "  if (meta) {\n"
            "    try { body.metadata = JSON.parse(meta); }\n"
            "    catch { throw new Error('metadata must be valid JSON'); }\n"
            "  }\n"
            "  return body;\n"
            "}\n\n"
            "export async function startVoice(form: HTMLFormElement) {\n"
            "  const res = await fetch('/api/speko-session', {\n"
            "    method: 'POST',\n"
            "    headers: { 'Content-Type': 'application/json' },\n"
            "    body: JSON.stringify(buildSessionBody(form)),\n"
            "  });\n"
            "  if (!res.ok) throw new Error(`session ${res.status}: ${await res.text()}`);\n"
            "  const { transportToken, transportUrl } = await res.json();\n\n"
            "  return VoiceConversation.create({\n"
            "    transportToken,\n"
            "    transportUrl,\n"
            "    onConnect: ({ conversationId }) => console.log('connected', conversationId),\n"
            "    onMessage: ({ source, text, isFinal }) => console.log(source, text, isFinal),\n"
            "    onStatusChange: (status) => console.log('status', status),\n"
            "    onModeChange: (mode) => console.log('mode', mode), // listening|speaking\n"
            "    onError: (err) => console.error(err),\n"
            "  });\n"
            "}\n"
            "```\n"
        ),
        Message(
            "Next steps: (1) author the config form in `index.html` with "
            "each input named exactly as above (e.g. "
            '`<input name="intent.language">`) so `buildSessionBody` '
            "picks them up; (2) wire `startVoice(form)` to the Start "
            "button's click handler (iOS `AudioContext` needs a user "
            "gesture); (3) surface mic-permission errors via `onError`. "
            "Field reference is `spekoai://docs/llms-full`. Deeper questions: "
            "`docs.search('<your-term>')`."
        ),
    ]


def _batch_transcribe_messages(language: Language, runtime: Runtime) -> list[Message]:
    if language == "python":
        return [
            Message(
                "Scaffold a Python batch-transcription job using SpekoAI.\n"
                "Read first: `spekoai://docs/llms-full` for the current API "
                "surface, then `spekoai://docs/sdk-python-readme` for a "
                "package walkthrough."
            ),
            Message(
                "Install: `pip install spekoai` (or `uv add spekoai`).\n"
                "Set `SPEKO_API_KEY` in your environment.\n\n"
                "Starter (`batch_transcribe.py`):\n\n"
                "```python\n"
                "import os\n"
                "from pathlib import Path\n"
                "from spekoai import Speko\n\n"
                "def main(files: list[Path]) -> None:\n"
                "    speko = Speko(api_key=os.environ['SPEKO_API_KEY'])\n"
                "    with speko:\n"
                "        for path in files:\n"
                "            audio = path.read_bytes()\n"
                "            result = speko.transcribe(\n"
                "                audio,\n"
                "                language='en',\n"
                "            )\n"
                "            print(path.name, '->', result.text)\n\n"
                "if __name__ == '__main__':\n"
                "    import sys\n"
                "    main([Path(p) for p in sys.argv[1:]])\n"
                "```\n"
            ),
            Message(
                "For large jobs, swap to `AsyncSpeko` and gather concurrently "
                "to stay under your rate limit. `SpekoRateLimitError.retry_after` "
                "tells you seconds to sleep."
            ),
        ]
    install = _install_cmd(runtime, ["@spekoai/sdk"])
    return [
        Message(
            "Scaffold a Node batch-transcription job using SpekoAI.\n"
            "Read first: `spekoai://docs/llms-full` and "
            "`spekoai://docs/sdk-readme`."
        ),
        Message(
            f"Install: `{install}`.\n"
            "Set `SPEKO_API_KEY` in your environment.\n\n"
            "Starter (`batch-transcribe.ts`):\n\n"
            "```ts\n"
            "import { Speko } from '@spekoai/sdk';\n"
            "import { readFile } from 'node:fs/promises';\n\n"
            "const speko = new Speko({ apiKey: process.env.SPEKO_API_KEY! });\n\n"
            "export async function transcribeAll(paths: string[]) {\n"
            "  for (const path of paths) {\n"
            "    const audio = await readFile(path);\n"
            "    const result = await speko.transcribe(audio, {\n"
            "      language: 'en',\n"
            "    });\n"
            "    console.log(path, '->', result.text);\n"
            "  }\n"
            "}\n"
            "```\n"
        ),
        Message(
            "For large jobs, parallelise with `Promise.all` bounded by a "
            "small concurrency limit, and pass an `AbortSignal` to "
            "`transcribe()` if you need to cancel a batch mid-run."
        ),
    ]


def _livekit_agent_messages(runtime: Runtime) -> list[Message]:
    install = _install_cmd(
        runtime,
        [
            "@spekoai/sdk",
            "@spekoai/adapter-livekit",
            "@livekit/agents",
            "@livekit/agents-plugin-silero",
            "@livekit/rtc-node",
        ],
    )
    return [
        Message(
            "Scaffold a LiveKit Agents worker that routes STT/LLM/TTS "
            "through SpekoAI.\n"
            "Read first: `spekoai://docs/llms-full` and "
            "`spekoai://docs/adapter-livekit-readme`. Pay attention to the "
            "v1 limitations section (buffered, not streaming; MP3 TTS is "
            "rejected)."
        ),
        Message(
            f"Install:\n```sh\n{install}\n```\n"
            "`@livekit/agents`, `@livekit/rtc-node`, and the silero plugin "
            "are peer deps — pin the versions you want to run against."
        ),
        Message(
            "Starter (`agent.ts`):\n\n"
            "```ts\n"
            "import {\n"
            "  type JobContext, type JobProcess, ServerOptions, cli,\n"
            "  defineAgent, voice,\n"
            "} from '@livekit/agents';\n"
            "import * as silero from '@livekit/agents-plugin-silero';\n"
            "import { Speko } from '@spekoai/sdk';\n"
            "import { createSpekoComponents } from '@spekoai/adapter-livekit';\n"
            "import { fileURLToPath } from 'node:url';\n\n"
            "const speko = new Speko({ apiKey: process.env.SPEKO_API_KEY! });\n\n"
            "export default defineAgent({\n"
            "  prewarm: async (proc: JobProcess) => {\n"
            "    proc.userData.vad = await silero.VAD.load();\n"
            "  },\n"
            "  entry: async (ctx: JobContext) => {\n"
            "    const vad = ctx.proc.userData.vad as silero.VAD;\n"
            "    const { stt, llm, tts } = createSpekoComponents({\n"
            "      speko, vad,\n"
            "      intent: { language: 'en-US' },\n"
            "    });\n"
            "    const session = new voice.AgentSession({ vad, stt, llm, tts });\n"
            "    await session.start({\n"
            "      agent: new voice.Agent({\n"
            "        instructions: 'Be a concise voice assistant.',\n"
            "      }),\n"
            "      room: ctx.room,\n"
            "    });\n"
            "    await ctx.connect();\n"
            "    session.generateReply({\n"
            "      instructions: 'Greet the user.',\n"
            "    });\n"
            "  },\n"
            "});\n\n"
            "cli.runApp(new ServerOptions({\n"
            "  agent: fileURLToPath(import.meta.url),\n"
            "  agentName: 'speko-demo',\n"
            "}));\n"
            "```\n"
        ),
        Message(
            "Gotcha: if TTS routing lands on ElevenLabs (MP3), the adapter "
            "throws. Pin TTS to Cartesia with "
            "`constraints: { allowedProviders: { tts: ['cartesia'] } }` on "
            "`createSpekoComponents`."
        ),
    ]


def _quickstart_messages(language: Language, runtime: Runtime) -> list[Message]:
    if language == "python":
        return [
            Message(
                "Scaffold the Python SpekoAI quickstart — hit "
                "transcribe, complete, synthesize once each.\n"
                "Read first: `spekoai://docs/llms-full` and "
                "`spekoai://docs/sdk-python-readme`."
            ),
            Message(
                "Install: `pip install spekoai`. Set `SPEKO_API_KEY`.\n\n"
                "Starter (`quickstart.py`):\n\n"
                "```python\n"
                "import os\n"
                "from pathlib import Path\n"
                "from spekoai import Speko\n\n"
                "speko = Speko(api_key=os.environ['SPEKO_API_KEY'])\n\n"
                "# 1) Complete\n"
                "reply = speko.complete(\n"
                "    messages=[{'role': 'user', 'content': 'Hi!'}],\n"
                "    intent={'language': 'en'},\n"
                ")\n"
                "print('complete:', reply.text, reply.provider)\n\n"
                "# 2) Synthesize\n"
                "speech = speko.synthesize('Hello world', language='en')\n"
                "ext = 'mp3' if 'mpeg' in speech.content_type else 'pcm'\n"
                "Path(f'out.{ext}').write_bytes(speech.audio)\n"
                "print('synthesize:', speech.provider, len(speech.audio), 'bytes')\n\n"
                "# 3) Transcribe (if you have a wav handy)\n"
                "path = Path('sample.wav')\n"
                "if path.exists():\n"
                "    stt = speko.transcribe(path.read_bytes(), language='en')\n"
                "    print('transcribe:', stt.text, stt.provider)\n"
                "```\n"
            ),
        ]
    install = _install_cmd(runtime, ["@spekoai/sdk"])
    return [
        Message(
            "Scaffold the Node SpekoAI quickstart — hit transcribe, "
            "complete, synthesize once each.\n"
            "Read first: `spekoai://docs/llms-full`, "
            "`spekoai://docs/sdk-readme`, and "
            "`spekoai://docs/quickstart-node-readme`. The existing "
            "quickstart under `packages/sdk/examples/quickstart-node/` is "
            "a great reference — the resource "
            "`spekoai://docs/quickstart-node-index-ts` has its full "
            "source inlined."
        ),
        Message(
            f"Install: `{install}`. Set `SPEKO_API_KEY` (and `SPEKO_BASE_URL` "
            "if you're pointing at a local/staging server).\n\n"
            "Starter (`quickstart.ts`):\n\n"
            "```ts\n"
            "import { Speko } from '@spekoai/sdk';\n"
            "import { readFile, writeFile } from 'node:fs/promises';\n\n"
            "const speko = new Speko({ apiKey: process.env.SPEKO_API_KEY! });\n\n"
            "const reply = await speko.complete({\n"
            "  messages: [{ role: 'user', content: 'Hi!' }],\n"
            "  intent: { language: 'en' },\n"
            "});\n"
            "console.log('complete:', reply.text, reply.provider);\n\n"
            "const speech = await speko.synthesize('Hello world', {\n"
            "  language: 'en',\n"
            "});\n"
            "const ext = speech.contentType.includes('mpeg') ? 'mp3' : 'pcm';\n"
            "await writeFile(`out.${ext}`, Buffer.from(speech.audio));\n"
            "console.log('synthesize:', speech.provider, speech.audio.byteLength, 'bytes');\n"
            "```\n"
        ),
    ]


def register_prompts(mcp: FastMCP) -> None:
    # Inline Literal types on the decorated signature — FastMCP resolves
    # the prompt's argument types via Pydantic TypeAdapter, which cannot
    # see module-level `Scenario = Literal[...]` aliases under
    # `from __future__ import annotations`. Inlining sidesteps the
    # forward-ref resolution issue without disabling future-annotations.
    @mcp.prompt(
        name="scaffold_project",
        title="Scaffold a SpekoAI project",
        description=(
            "Step-by-step scaffold for a SpekoAI project. "
            "Scenarios: voice_conversation (browser + backend, TS), "
            "batch_transcribe (TS or Python), livekit_agent (TS), "
            "quickstart (TS or Python). voice_conversation and "
            "livekit_agent are TypeScript-only; choose "
            "language='typescript' for those."
        ),
    )
    def scaffold_project(
        scenario: Literal[
            "voice_conversation",
            "batch_transcribe",
            "livekit_agent",
            "quickstart",
        ],
        language: Literal["typescript", "python"] = "typescript",
        runtime: Literal["bun", "node", "deno"] = "node",
    ) -> list[Message]:
        if scenario in _TS_ONLY_SCENARIOS and language == "python":
            suggestion = "quickstart" if scenario == "voice_conversation" else "batch_transcribe"
            raise PromptError(
                f"scenario={scenario!r} is TypeScript-only today "
                "(no Python browser client; "
                "@spekoai/adapter-livekit is TS-only). "
                f"Use language='typescript', or pick scenario={suggestion!r} "
                "for a Python-compatible scaffold."
            )

        if scenario == "voice_conversation":
            return _voice_conversation_messages(runtime)
        if scenario == "batch_transcribe":
            return _batch_transcribe_messages(language, runtime)
        if scenario == "livekit_agent":
            return _livekit_agent_messages(runtime)
        if scenario == "quickstart":
            return _quickstart_messages(language, runtime)
        raise PromptError(f"unknown scenario: {scenario!r}")  # pragma: no cover

    @mcp.prompt(
        name="migrate_voice_agent",
        title="Migrate a voice agent to Speko",
        description=(
            "Guided migration from LiveKit, Pipecat, Retell, or Vapi to "
            "Speko. Reads the provider-specific migration guide, inspects "
            "the codebase, converts config when available, maps tools, "
            "tests, and deploys only after user confirmation."
        ),
    )
    def migrate_voice_agent(
        from_platform: Literal["livekit", "pipecat", "retell", "vapi"],
        workspace_root: str = ".",
        config_path: str | None = None,
        runtime: Literal["node", "bun", "deno", "python"] = "node",
    ) -> list[Message]:
        guide_uri = f"spekoai://docs/migration-{from_platform}"
        config_step = (
            f"Read `{config_path}`, call `migration.external_config.parse("
            f'format="{from_platform}", raw=<file contents>)`, and inspect '
            "`unmappable_tools`."
            if config_path
            else (
                "No config_path was provided. First inspect the codebase and "
                "ask the user for the source config path if one exists; do "
                "not call `migration.external_config.parse` with a guessed path."
            )
        )
        adapter_warning = ""
        if from_platform == "retell":
            adapter_warning = (
                "\n\nRetell warning: `@spekoai/adapter-retell` is scaffold-only. "
                "Do not treat it as a production adapter; migrate through "
                "Speko SDK/platform APIs."
            )
        if from_platform == "vapi":
            adapter_warning = (
                "\n\nVapi warning: `@spekoai/adapter-vapi` is scaffold-only. "
                "Do not treat it as a production adapter; migrate through "
                "Speko SDK/platform APIs."
            )

        return [
            Message(
                "Migrate this voice agent to Speko.\n\n"
                f"Source platform: `{from_platform}`\n"
                f"Workspace root: `{workspace_root}`\n"
                f"Runtime target: `{runtime}`\n"
                f"Migration guide: `{guide_uri}`\n\n"
                f"Before editing code, read the bundled guide via the "
                f"`{guide_uri}` resource or "
                f'`docs.search("{from_platform} migration")`. Read local '
                "guide/source docs too if they are present in the repository."
                f"{adapter_warning}"
            ),
            Message(
                "Inspection flow:\n"
                f"1. Inspect `{workspace_root}` yourself with normal codebase "
                "search/read tools. Identify current providers, prompts, "
                "session creation, transport, tool/function callbacks, and "
                "deployment path.\n"
                "2. If authenticated Speko MCP tools are available, call "
                f'`migration.workspace.inspect(workspace_root="{workspace_root}", '
                "deep=false)` to get platform-side recommendations.\n"
                f"3. Read `{guide_uri}` again before deciding code changes."
            ),
            Message(
                "Config conversion:\n"
                f"{config_step}\n\n"
                "If `unmappable_tools` is non-empty, stop and map each item "
                "explicitly to a Speko webhook tool, builtin tool, or "
                "SDK-side handler. Never silently drop tools or pretend a "
                "provider-specific function was migrated."
            ),
            Message(
                "Code migration:\n"
                "- Update session creation to Speko SDK/client/platform APIs.\n"
                "- Move provider choices into `intent`, `llmOptions`, "
                "`stackPreferences`, and `sttOptions` instead of direct "
                "provider SDK calls.\n"
                "- Keep secrets server-side; never expose `SPEKO_API_KEY` "
                "to browser/client code.\n"
                "- Preserve business logic, tool schemas, webhook contracts, "
                "and tests where possible.\n"
                "- For LiveKit, use `@spekoai/adapter-livekit` when keeping "
                "LiveKit as the worker runtime. For Pipecat, Retell, and "
                "Vapi, migrate through Speko SDK/platform APIs."
            ),
            Message(
                "Validation and deploy gate:\n"
                "1. Run the repo's local tests/lint/build for edited code.\n"
                "2. Call `sessions.create` against the draft or deployed agent.\n"
                "3. Inspect failures with `agents.calls.list` and `calls.get`.\n"
                "4. Add regression evals with `agents.evals.create` "
                "for failed calls worth preserving.\n"
                "5. Ask the user for explicit confirmation before calling "
                "`agents.deploy`. "
                "Do not deploy automatically."
            ),
        ]
