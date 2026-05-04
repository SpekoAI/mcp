"""Use-case-aware Next.js App Router scaffold for SpekoAI voice apps.

`scaffold_voice_app(use_case, languages?, system_prompt?)` returns a
`ScaffoldManifest` — a strict list of files the agent should create,
install commands to run, and env vars to set.

Fixed stack: Next.js App Router (TypeScript), Node runtime, `@spekoai/client`
in the browser. The scaffolded backend route calls
`POST https://api.speko.ai/v1/sessions` via raw fetch (the server SDK
doesn't expose a sessions helper yet — see sdk-skills.md). Keeping the
raw fetch in the template means the scaffold works with just
`@spekoai/client` installed; SDK usage stays an upgrade path.

Spoken languages are limited to English + Spanish for v1. The Speko
router handles inter-turn language switching at runtime; we pick one
value for `intent.language` at session creation and let the system
prompt tell the agent to match the caller from there.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, Field

from spekoai_mcp.selector import OptimizeFor, select_ranked

SpokenLanguage = Literal["en", "es"]
Framework = Literal["nextjs"]


class ScaffoldFile(BaseModel):
    path: str = Field(
        description="Project-root-relative path (e.g. app/api/speko/route.ts)."
    )
    content: str = Field(description="Full file body.")
    language_hint: str = Field(description="Syntax hint: ts, tsx, env, json, ...")
    action: Literal["create", "append", "merge"] = Field(default="create")


class EnvVar(BaseModel):
    name: str
    description: str
    required: bool
    example: str | None = None


class ScaffoldManifest(BaseModel):
    files: list[ScaffoldFile]
    install_commands: list[str]
    env_vars: list[EnvVar]
    post_install_steps: list[str]
    docs_resources: list[str] = Field(
        description="spekoai://docs/... URIs to read for deeper context."
    )
    component_resources: list[str] = Field(
        description="spekoai://components/... URIs the scaffold inlines from."
    )


# Single neutral system prompt — vertical-tuned prompts will land once the
# benchmark data is per-vertical. Until then we ship a generic, well-
# defined voice-assistant baseline so the scaffold compiles and runs;
# the caller is expected to overwrite this for their domain via the
# `system_prompt` parameter.
_DEFAULT_SYSTEM_PROMPT = (
    "You are a concise, helpful voice assistant. Answer the caller's "
    "questions directly, ask one clarifying question at a time when "
    "you need more context, and confirm numbers, names, and dates by "
    "repeating them back to avoid mishearing. Edit this prompt to "
    "match your domain — Speko routes the audio; the prompt defines "
    "the persona."
)

_MULTILINGUAL_APPEND = (
    " Reply in whichever language the caller uses — both English and "
    "Spanish are supported."
)

_LANGUAGE_TAG: dict[SpokenLanguage, str] = {
    "en": "en-US",
    "es": "es-US",
}


def _default_system_prompt(languages: list[SpokenLanguage]) -> str:
    base = _DEFAULT_SYSTEM_PROMPT
    if "es" in languages:
        return base + _MULTILINGUAL_APPEND
    return base


def _route_ts(
    system_prompt: str,
    language_tag: str,
    optimize_for: OptimizeFor = "latency",
    region: str = "global",
) -> str:
    escaped_prompt = system_prompt.replace("\\", "\\\\").replace("`", "\\`")

    # When either knob is non-default, bake it into the route handler.
    # `latency` is the runtime default for voice apps — we leave the
    # constant commented out so the scaffold's default body stays
    # minimal; the Speko router infers the same axis when omitted.
    region_line = (
        f"const DEFAULT_REGION = '{region}';"
        if region != "global"
        else "// const DEFAULT_REGION = 'us-east4'; // omit to use 'global' (batch ranking)"
    )
    optimize_line = (
        f"const DEFAULT_OPTIMIZE_FOR: 'latency' | 'accuracy' | 'cost' = '{optimize_for}';"
        if optimize_for != "latency"
        else "// const DEFAULT_OPTIMIZE_FOR: 'latency' | 'accuracy' | 'cost' = 'accuracy';"
    )

    # Run a fresh selector pass so the header comment lists the
    # top-1 picks the runtime would route to for this (optimize_for,
    # region) pair. English-only today; the comment carries an
    # explicit caveat so callers see when picks were unavailable.
    picks_header_lines: list[str] = [
        "// === Speko routing picks (top-1 from bundled v0 fixtures) ===",
        f"// optimize_for={optimize_for}, region={region}",
    ]
    selection = select_ranked(
        language="en", region=region, optimize_for=optimize_for, limit=1
    )
    for label, picks in (
        ("STT", selection.stt),
        ("LLM", selection.llm),
        ("TTS", selection.tts),
        ("S2S", selection.s2s),
    ):
        if picks:
            top = picks[0]
            picks_header_lines.append(
                f"//   {label}: {top.provider_id} (score={top.score:.3f}, p50={top.primary_latency_ms}ms)"
            )
        else:
            picks_header_lines.append(f"//   {label}: (no fixture data)")
    picks_header_lines.append("// ============================================================")
    picks_header = "\n".join(picks_header_lines)

    return f"""// Next.js App Router route handler — mints a Speko conversation
// token for the browser. The Speko server SDK does not expose a
// sessions helper yet, so this uses raw fetch against /v1/sessions.
//
// Accepts LiveKit's standard TokenSource request body (room_name,
// participant_identity, ...) and ignores it (Speko manages the room
// internally). Speko-side config — systemPrompt, intent.language,
// intent.region, intent.optimizeFor — is set below and may be
// overridden per-request by adding the same fields to the body.
//
// Returns `{{ server_url, participant_token }}` so it plugs directly
// into LiveKit's TokenSource.endpoint() on the client.

{picks_header}

export const runtime = 'nodejs';

const SPEKO_API_KEY = process.env.SPEKO_API_KEY;
const SPEKO_BASE_URL = process.env.SPEKO_BASE_URL ?? 'https://api.speko.ai';

// === Speko session config ===================================================
// Customize these to tune the assistant persona, language routing, and
// latency/quality tradeoff. Anything declared here is the default; the
// client can override any field per-request by sending it in the POST body.
const DEFAULT_SYSTEM_PROMPT = `{escaped_prompt}`;
const DEFAULT_LANGUAGE = '{language_tag}';
{region_line}
{optimize_line}
// ============================================================================

type SessionOverrides = {{
  intent?: {{
    language?: string;
    region?: string;
    optimizeFor?: 'latency' | 'quality';
  }};
  systemPrompt?: string;
}};

export async function POST(req: Request): Promise<Response> {{
  if (!SPEKO_API_KEY) {{
    return new Response('SPEKO_API_KEY not set', {{ status: 500 }});
  }}

  let override: SessionOverrides = {{}};
  try {{
    const raw = await req.text();
    if (raw) override = JSON.parse(raw) as SessionOverrides;
  }} catch {{
    return new Response('invalid JSON body', {{ status: 400 }});
  }}

  const body = {{
    intent: {{
      language: override.intent?.language ?? DEFAULT_LANGUAGE,
      ...(override.intent?.region && {{ region: override.intent.region }}),
      ...(override.intent?.optimizeFor && {{
        optimizeFor: override.intent.optimizeFor,
      }}),
    }},
    systemPrompt: override.systemPrompt ?? DEFAULT_SYSTEM_PROMPT,
  }};

  const res = await fetch(`${{SPEKO_BASE_URL}}/v1/sessions`, {{
    method: 'POST',
    headers: {{
      Authorization: `Bearer ${{SPEKO_API_KEY}}`,
      'Content-Type': 'application/json',
    }},
    body: JSON.stringify(body),
  }});

  if (!res.ok) {{
    const detail = await res.text();
    return new Response(`speko /v1/sessions ${{res.status}}: ${{detail}}`, {{
      status: res.status,
    }});
  }}

  const {{ conversationToken, livekitUrl }} = (await res.json()) as {{
    conversationToken: string;
    livekitUrl: string;
  }};

  // LiveKit's TokenSource.endpoint() expects this exact shape.
  return Response.json(
    {{ server_url: livekitUrl, participant_token: conversationToken }},
    {{ status: 201 }},
  );
}}
"""


def _page_tsx(
    system_prompt: str,
    language_tag: str,
    optimize_for: OptimizeFor = "latency",
) -> str:
    escaped_prompt = system_prompt.replace("\\", "\\\\").replace("`", "\\`")
    # Page is a Server Component that hands initial config (read from
    # env-agnostic constants, not env vars) down to the client island.
    # Users edit these defaults here and in app/api/speko/route.ts
    # — the route-level defaults still apply when the client omits a
    # field, but the UI always sends explicit values.
    #
    # `optimize_for` is injected verbatim into the page's DEFAULT_CONFIG
    # so the pre-call form ships with the same intent the caller asked
    # for.
    page_optimize = optimize_for
    return f"""import {{ SpekoVoiceSession }} from '@/components/speko-voice-session';

const DEFAULT_CONFIG = {{
  language: '{language_tag}' as const,
  optimizeFor: '{page_optimize}' as const,
  systemPrompt: `{escaped_prompt}`,
}};

export default function Page() {{
  return (
    <main className="relative min-h-svh bg-[#FFFBF5] text-[#1C1917]">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_80%_at_50%_0%,#FDE3CC_0%,transparent_55%)]"
      />
      <div className="relative mx-auto flex min-h-svh max-w-5xl flex-col items-center justify-center gap-10 px-6 py-16">
        <header className="space-y-3 text-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-[#FDE3CC] bg-white/60 px-3 py-1 text-xs font-medium uppercase tracking-wider text-[#C2410C]">
            <span className="size-1.5 rounded-full bg-[#E8590C]" />
            Speko Voice Demo
          </span>
          <h1 className="text-balance text-4xl font-semibold tracking-tight text-[#0C0A09] sm:text-5xl">
            Talk to a voice agent, live.
          </h1>
          <p className="mx-auto max-w-prose text-pretty text-base text-[#57534E]">
            STT &rarr; LLM &rarr; TTS routed through the Speko gateway. Pick a language, then start the call.
          </p>
        </header>
        <SpekoVoiceSession defaults={{DEFAULT_CONFIG}} className="w-full" />
      </div>
    </main>
  );
}}
"""  # noqa: E501


def _layout_tsx() -> str:
    return """import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Speko Voice Demo',
  description: 'Realtime voice AI powered by the Speko gateway.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
"""


def _env_example() -> str:
    return """# Get an API key at https://dashboard.speko.ai/api-keys
SPEKO_API_KEY=

# Optional — override if you're targeting a local/staging Speko server.
# SPEKO_BASE_URL=https://api.speko.ai
"""


def _load_react_voice_session() -> str:
    return (
        files("spekoai_mcp._components") / "react_voice_session.tsx"
    ).read_text(encoding="utf-8")


def build_voice_app_manifest(
    languages: list[SpokenLanguage] | None = None,
    system_prompt: str | None = None,
    optimize_for: OptimizeFor = "latency",
    region: str = "global",
) -> ScaffoldManifest:
    """Build a Next.js App Router voice-app scaffold for any domain.

    `optimize_for` and `region` are forwarded into the route handler:
    when non-default the scaffold bakes the values in (the constants
    live commented out in the default template). The route file's
    header comment lists the top-1 STT/LLM/TTS/S2S provider picks for
    the requested (optimize_for, region) pair so the user sees what
    routing they're shipping before they hit deploy.
    """
    langs: list[SpokenLanguage] = list(languages) if languages else ["en"]
    # Deduplicate while preserving order so ["en", "en"] collapses to ["en"]
    # without changing the primary language choice.
    seen: set[SpokenLanguage] = set()
    langs = [lang for lang in langs if not (lang in seen or seen.add(lang))]
    if not langs:
        langs = ["en"]

    prompt = (
        system_prompt
        if system_prompt is not None
        else _default_system_prompt(langs)
    )
    primary_language_tag = _LANGUAGE_TAG[langs[0]]

    files_list = [
        ScaffoldFile(
            path="app/api/speko/route.ts",
            content=_route_ts(
                prompt, primary_language_tag, optimize_for=optimize_for, region=region
            ),
            language_hint="ts",
        ),
        ScaffoldFile(
            path="components/speko-voice-session.tsx",
            content=_load_react_voice_session(),
            language_hint="tsx",
        ),
        ScaffoldFile(
            path="app/page.tsx",
            content=_page_tsx(prompt, primary_language_tag, optimize_for=optimize_for),
            language_hint="tsx",
        ),
        ScaffoldFile(
            path="app/layout.tsx",
            content=_layout_tsx(),
            language_hint="tsx",
        ),
        ScaffoldFile(
            path=".env.example",
            content=_env_example(),
            language_hint="env",
        ),
    ]

    return ScaffoldManifest(
        files=files_list,
        install_commands=[
            "npm install @spekoai/client",
            "npx -y shadcn@latest init --yes --base-color stone",
            "npx -y shadcn@latest add button card label select textarea",
        ],
        env_vars=[
            EnvVar(
                name="SPEKO_API_KEY",
                description="Speko API key, used by the backend route to mint session tokens.",
                required=True,
                example="sk_...",
            ),
            EnvVar(
                name="SPEKO_BASE_URL",
                description="Speko API base URL. Default is https://api.speko.ai.",
                required=False,
                example="https://api.speko.ai",
            ),
        ],
        post_install_steps=[
            "Set SPEKO_API_KEY in .env.local (copy from .env.example).",
            "Open app/globals.css and delete the `html { @apply font-sans; }` "
            "block that `shadcn init` injects — it collides with Next.js' "
            "default sans font wiring and shows up as a Tailwind build error.",
            "Run `npm run dev` and open http://localhost:3000.",
            "Click 'Start conversation' and grant microphone permission.",
            "The pre-call config panel (language / optimizeFor / "
            "systemPrompt) lives in components/speko-voice-session.tsx and "
            "its form defaults are seeded from DEFAULT_CONFIG in "
            "app/page.tsx. Edit either to change what the caller sees on "
            "first load; the route-level defaults in app/api/speko/"
            "route.ts are the fallback when the client omits a field. See "
            "spekoai://docs/client-skills for the full /v1/sessions schema.",
            "components/speko-voice-session.tsx uses @spekoai/client's "
            "`VoiceConversation.create({conversationToken, livekitUrl, ...})` "
            "and renders the transcript + mode indicator from its callbacks. "
            "See spekoai://docs/client-skills for the full callback surface "
            "(onMessage, onStatusChange, onModeChange, onError).",
        ],
        docs_resources=[
            "spekoai://docs/client-skills",
            "spekoai://docs/client-readme",
            "spekoai://docs/sdk-skills",
        ],
        component_resources=[
            "spekoai://components/react/voice-session",
        ],
    )
