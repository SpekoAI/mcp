"""Vertical-aware Next.js App Router scaffold for SpekoAI voice apps.

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

from spekoai_mcp.recommendations import UseCase

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


_SYSTEM_PROMPTS: dict[UseCase, str] = {
    "healthcare": (
        "You are a voice assistant for a healthcare provider. Be concise "
        "and empathetic. Capture chief complaint, current symptoms, and "
        "any medications the caller mentions. Never give a diagnosis or "
        "definitive medical advice — always recommend the caller speak "
        "to a licensed clinician. Confirm key medical details (dosage, "
        "drug names) by repeating them back to avoid mishearing."
    ),
    "insurance": (
        "You are a voice assistant for an insurance provider. Help the "
        "caller file a claim, check coverage, or navigate forms. Be "
        "clear about policy limits and deductibles. Never promise "
        "coverage — tell the caller eligibility is confirmed by the "
        "underwriter. Capture policy number, date of incident, and a "
        "short incident description. Repeat numbers back to confirm."
    ),
    "financial_services": (
        "You are a voice assistant for a financial services firm. Help "
        "with account questions, transaction history, and basic banking "
        "inquiries. Do not give investment advice. Verify caller "
        "identity out-of-band before discussing account details. Repeat "
        "amounts and account IDs back to confirm."
    ),
    "support_agent": (
        "You are a global customer support voice assistant. Be concise, "
        "helpful, and empathetic. Solve the caller's problem in as few "
        "turns as possible, or escalate to a human agent when the issue "
        "is complex. Match the caller's language; reply in whichever "
        "language they use."
    ),
}

# Verticals whose default prompt already addresses multilingual behavior;
# skip the generic EN/ES append for these so we don't double up.
_ALREADY_MULTILINGUAL: set[UseCase] = {"support_agent"}

_MULTILINGUAL_APPEND = (
    " Reply in whichever language the caller uses — both English and "
    "Spanish are supported."
)

_LANGUAGE_TAG: dict[SpokenLanguage, str] = {
    "en": "en-US",
    "es": "es-US",
}


def _default_system_prompt(
    use_case: UseCase, languages: list[SpokenLanguage]
) -> str:
    base = _SYSTEM_PROMPTS[use_case]
    if "es" in languages and use_case not in _ALREADY_MULTILINGUAL:
        return base + _MULTILINGUAL_APPEND
    return base


def _route_ts(system_prompt: str, language_tag: str) -> str:
    escaped_prompt = system_prompt.replace("\\", "\\\\").replace("`", "\\`")
    return f"""// Next.js App Router route handler — mints a Speko conversation
// token for the browser. The Speko server SDK does not expose a
// sessions helper yet, so this uses raw fetch against /v1/sessions.
// Node runtime is required: the request handler reads env vars and the
// Speko API returns JSON via Node-style fetch.

export const runtime = 'nodejs';

const SPEKO_API_KEY = process.env.SPEKO_API_KEY;
const SPEKO_BASE_URL = process.env.SPEKO_BASE_URL ?? 'https://api.speko.ai';

const DEFAULT_SYSTEM_PROMPT = `{escaped_prompt}`;
const DEFAULT_LANGUAGE = '{language_tag}';
const DEFAULT_VERTICAL = 'general';

type SessionConfig = {{
  intent?: {{
    language?: string;
    vertical?: string;
    optimizeFor?: string;
  }};
  systemPrompt?: string;
  [key: string]: unknown;
}};

export async function POST(req: Request): Promise<Response> {{
  if (!SPEKO_API_KEY) {{
    return new Response('SPEKO_API_KEY not set', {{ status: 500 }});
  }}

  let override: SessionConfig = {{}};
  try {{
    const raw = await req.text();
    if (raw) override = JSON.parse(raw) as SessionConfig;
  }} catch {{
    return new Response('invalid JSON body', {{ status: 400 }});
  }}

  const body: SessionConfig = {{
    ...override,
    intent: {{
      language: override.intent?.language ?? DEFAULT_LANGUAGE,
      vertical: override.intent?.vertical ?? DEFAULT_VERTICAL,
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
  return Response.json({{ conversationToken, livekitUrl }});
}}
"""


def _page_tsx() -> str:
    return """'use client';

import { SpekoVoiceSession } from '../../components/VoiceSession';

export default function VoicePage() {
  return (
    <main style={{ padding: '2rem', fontFamily: 'system-ui' }}>
      <h1>Speko voice demo</h1>
      <SpekoVoiceSession
        sessionEndpoint="/api/speko"
        onError={(err) => console.error(err)}
      />
    </main>
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
    use_case: UseCase,
    languages: list[SpokenLanguage] | None = None,
    system_prompt: str | None = None,
) -> ScaffoldManifest:
    """Build a Next.js App Router voice-app scaffold for one Speko vertical."""
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
        else _default_system_prompt(use_case, langs)
    )
    primary_language_tag = _LANGUAGE_TAG[langs[0]]

    files_list = [
        ScaffoldFile(
            path="app/api/speko/route.ts",
            content=_route_ts(prompt, primary_language_tag),
            language_hint="ts",
        ),
        ScaffoldFile(
            path="components/VoiceSession.tsx",
            content=_load_react_voice_session(),
            language_hint="tsx",
        ),
        ScaffoldFile(
            path="app/voice/page.tsx",
            content=_page_tsx(),
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
        install_commands=["npm install @spekoai/client @spekoai/sdk"],
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
            "Run `npm run dev` and open /voice.",
            "Click Start and grant microphone permission when prompted.",
            "Customize the backend route's defaults (systemPrompt, intent) "
            "or pass a `sessionBody` prop to <SpekoVoiceSession/> to "
            "override per-session. See spekoai://docs/client-skills for "
            "the full /v1/sessions request shape before adding fields.",
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
