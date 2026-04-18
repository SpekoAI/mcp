"""`scaffold_project` — the one MCP prompt.

Claude Code and similar clients surface MCP prompts as slash commands;
this one walks the agent through scaffolding a SpekoAI project end to
end. Each scenario emits a `list[Message]` that includes install
commands, a starter file, and explicit resource URIs for the agent to
read next — the heavy lifting is in `spekoai://docs/*`, not in the
message body.

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


_TS_ONLY_SCENARIOS: set[Scenario] = {"voice_conversation", "livekit_agent"}


def _install_cmd(runtime: Runtime, packages: list[str]) -> str:
    joined = " ".join(packages)
    if runtime == "bun":
        return f"bun add {joined}"
    if runtime == "deno":
        return f"deno add {' '.join(f'npm:{p}' for p in packages)}"
    return f"npm install {joined}"


def _voice_conversation_messages(runtime: Runtime) -> list[Message]:
    install = _install_cmd(runtime, ["@spekoai/client"])
    server_install = _install_cmd(runtime, ["@spekoai/sdk"])
    return [
        Message(
            "Scaffold a browser voice-conversation app using SpekoAI.\n"
            "Before writing code, read these resources for the authoritative "
            "API shapes and gotchas (don't paraphrase — read them):\n"
            "- `spekoai://docs/client-skills` (browser SDK skill sheet)\n"
            "- `spekoai://docs/sdk-skills` (server SDK — you'll need this "
            "to mint session tokens)\n"
            "- `spekoai://docs/client-readme` (full browser SDK reference)\n"
        ),
        Message(
            "Architecture: the browser calls `VoiceConversation.create({ "
            "conversationToken, livekitUrl, ... })`. Your backend issues "
            "the token by calling `POST /v1/sessions` on the Speko API "
            "(via `@spekoai/sdk` or a direct REST call). Never call "
            "`/v1/sessions` from the browser — that would leak your "
            "`SPEKO_API_KEY`.\n\n"
            f"Install — browser: `{install}`\n"
            f"Install — backend: `{server_install}`"
        ),
        Message(
            "Starter — browser (`src/voice.ts`):\n\n"
            "```ts\n"
            "import { VoiceConversation } from '@spekoai/client';\n\n"
            "export async function startVoice() {\n"
            "  const res = await fetch('/api/speko-session', { method: 'POST' });\n"
            "  const { conversationToken, livekitUrl } = await res.json();\n\n"
            "  const conversation = await VoiceConversation.create({\n"
            "    conversationToken,\n"
            "    livekitUrl,\n"
            "    onConnect: ({ conversationId }) =>\n"
            "      console.log('connected', conversationId),\n"
            "    onMessage: ({ source, text, isFinal }) =>\n"
            "      console.log(source, text, isFinal),\n"
            "    onStatusChange: (status) => console.log('status', status),\n"
            "    onModeChange: (mode) => console.log('mode', mode),\n"
            "    onError: (err) => console.error(err),\n"
            "  });\n"
            "  return conversation;\n"
            "}\n"
            "```\n"
        ),
        Message(
            "Next steps: (1) implement the `/api/speko-session` backend route "
            "that mints `conversationToken` + `livekitUrl`; (2) gate "
            "`startVoice()` on a user gesture so iOS `AudioContext` can start; "
            "(3) surface mic-permission errors via `onError`. If you hit a "
            "specific question, call the `search_docs` tool — e.g. "
            "`search_docs('ConversationOverrides')`."
        ),
    ]


def _batch_transcribe_messages(language: Language, runtime: Runtime) -> list[Message]:
    if language == "python":
        return [
            Message(
                "Scaffold a Python batch-transcription job using SpekoAI.\n"
                "Read first: `spekoai://docs/sdk-python-skills` for the API "
                "surface, then `spekoai://docs/sdk-python-readme` for a full "
                "walkthrough."
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
                "                vertical='general',\n"
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
            "Read first: `spekoai://docs/sdk-skills` and "
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
            "      vertical: 'general',\n"
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
            "Read first: `spekoai://docs/adapter-livekit-skills` and "
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
            "      intent: { language: 'en-US', vertical: 'general' },\n"
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
                "Read first: `spekoai://docs/sdk-python-skills` and "
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
                "    intent={'language': 'en', 'vertical': 'general'},\n"
                ")\n"
                "print('complete:', reply.text, reply.provider)\n\n"
                "# 2) Synthesize\n"
                "speech = speko.synthesize('Hello world', language='en', vertical='general')\n"
                "ext = 'mp3' if 'mpeg' in speech.content_type else 'pcm'\n"
                "Path(f'out.{ext}').write_bytes(speech.audio)\n"
                "print('synthesize:', speech.provider, len(speech.audio), 'bytes')\n\n"
                "# 3) Transcribe (if you have a wav handy)\n"
                "path = Path('sample.wav')\n"
                "if path.exists():\n"
                "    stt = speko.transcribe(path.read_bytes(), language='en', vertical='general')\n"
                "    print('transcribe:', stt.text, stt.provider)\n"
                "```\n"
            ),
        ]
    install = _install_cmd(runtime, ["@spekoai/sdk"])
    return [
        Message(
            "Scaffold the Node SpekoAI quickstart — hit transcribe, "
            "complete, synthesize once each.\n"
            "Read first: `spekoai://docs/sdk-skills`, "
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
            "  intent: { language: 'en', vertical: 'general' },\n"
            "});\n"
            "console.log('complete:', reply.text, reply.provider);\n\n"
            "const speech = await speko.synthesize('Hello world', {\n"
            "  language: 'en', vertical: 'general',\n"
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
        runtime: Literal["bun", "node", "deno"] = "bun",
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
