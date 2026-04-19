"""Opinionated stack recommendations for the four SpekoAI verticals.

The `recommended_stack` MCP tool wraps `recommend(use_case)` unchanged —
the rules-based logic here stays importable from tests without spinning
up a FastMCP server.

All four verticals target the same implementation stack today (Next.js
App Router, Node runtime, `@spekoai/client` in the browser,
`@spekoai/sdk` on the backend). The per-vertical surface is the
tagline, rationale, and the compliance/operational warnings an agent
needs to surface to the user before shipping a production build.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

UseCase = Literal[
    "healthcare",
    "insurance",
    "financial_services",
    "support_agent",
]


class PackageRecommendation(BaseModel):
    """One package the caller should install."""

    name: str = Field(description="Package name, e.g. @spekoai/client.")
    purpose: str = Field(description="One-line reason this package is in the stack.")
    required: bool = Field(
        description="True = core to the scaffold; False = nice-to-have."
    )
    install_command: str = Field(description="Copy-paste install command.")
    docs_uri: str | None = Field(
        default=None,
        description="spekoai://docs/{slug} pointer for deeper reading.",
    )


class StackRecommendation(BaseModel):
    """Opinionated stack for one Speko vertical."""

    use_case: UseCase
    tagline: str = Field(description="The speko.dev positioning line for this vertical.")
    packages: list[PackageRecommendation]
    rationale: str = Field(
        description="Why this stack fits the vertical; one paragraph."
    )
    warnings: list[str] = Field(
        description="Vertical-specific caveats (compliance, data retention, auth)."
    )
    next_tool: str = Field(
        description="Suggested follow-up MCP tool to call with the chosen use case."
    )
    related_resources: list[str] = Field(
        description="spekoai://docs/... URIs the agent should read first."
    )


_TAGLINES: dict[UseCase, str] = {
    "healthcare": "Clinical-grade accuracy — 98.5% medical-term accuracy.",
    "insurance": "Evidence-grade transcripts for claims.",
    "financial_services": "Audit-grade recording for regulated conversations.",
    "support_agent": "Global support — 10+ languages, routed live.",
}

_RATIONALES: dict[UseCase, str] = {
    "healthcare": (
        "Browser-based voice intake where mishearing a dosage or drug name "
        "is a safety event. @spekoai/client streams to Speko over WebRTC; "
        "the router prefers STT providers benchmarked on medical "
        "terminology. The backend mints short-lived conversation tokens "
        "so PHI never leaves your control plane."
    ),
    "insurance": (
        "Inbound claims intake where the transcript is the record of "
        "record. @spekoai/client captures the conversation; the "
        "server-side SDK can re-run transcription on the stored audio for "
        "adjuster review. The scaffold leaves the audio-retention "
        "decision to you — see the warning below."
    ),
    "financial_services": (
        "Customer-facing voice interactions that may be audited later. "
        "@spekoai/client handles the real-time conversation; the Speko "
        "API stores conversation metadata you can query for reporting. "
        "Identity verification is out-of-band — the voice agent must not "
        "be the sole auth factor."
    ),
    "support_agent": (
        "Multilingual customer support where the router switches STT/TTS "
        "providers per utterance based on the detected language. "
        "@spekoai/client handles the browser side; use the session's "
        "`intent.language` field to anchor the initial language, then let "
        "the system prompt tell the agent to match the caller."
    ),
}

_WARNINGS: dict[UseCase, list[str]] = {
    "healthcare": [
        "Not HIPAA-compliant out of the box — sign a BAA with Speko and "
        "your own hosting vendor before routing PHI through this stack.",
        "STT confidence is not a diagnosis confidence. Have a clinician "
        "review any automated triage decisions.",
    ],
    "insurance": [
        "Store conversation recordings immutably if they're used as claim "
        "evidence — the API returns audio URLs, it's on you to persist "
        "them with an append-only retention policy.",
        "Disclose recording at the start of the call per state-specific "
        "two-party-consent laws (e.g. CA, FL, IL, WA).",
    ],
    "financial_services": [
        "Verify caller identity out-of-band (e.g. one-time code, known "
        "device) before discussing account details — the voice agent has "
        "no inherent authentication.",
        "Keep an immutable call log if your regulator requires it "
        "(FINRA 4511, MiFID II). The Speko API gives you the transcript; "
        "retention is your responsibility.",
    ],
    "support_agent": [
        "Cross-language routing lives in the session config "
        "(`intent.language`). For true multilingual, surface a language "
        "switch in your UI and re-mint the session on change.",
        "Latency is higher on language-switch turns — warm up the "
        "likely second language via a contextual hint if you know it.",
    ],
}

_RELATED_RESOURCES: list[str] = [
    "spekoai://docs/client-skills",
    "spekoai://docs/client-readme",
    "spekoai://docs/sdk-skills",
]


def _default_packages() -> list[PackageRecommendation]:
    return [
        PackageRecommendation(
            name="@spekoai/client",
            purpose="Browser WebRTC voice session (VoiceConversation.create).",
            required=True,
            install_command="npm install @spekoai/client",
            docs_uri="spekoai://docs/client-skills",
        ),
        PackageRecommendation(
            name="@spekoai/sdk",
            purpose=(
                "Node backend: mint /v1/sessions tokens; "
                "optionally call transcribe/complete/synthesize."
            ),
            required=True,
            install_command="npm install @spekoai/sdk",
            docs_uri="spekoai://docs/sdk-skills",
        ),
    ]


def recommend(use_case: UseCase) -> StackRecommendation:
    """Return the opinionated SpekoAI stack for one vertical."""
    return StackRecommendation(
        use_case=use_case,
        tagline=_TAGLINES[use_case],
        packages=_default_packages(),
        rationale=_RATIONALES[use_case],
        warnings=list(_WARNINGS[use_case]),
        next_tool="scaffold_voice_app",
        related_resources=list(_RELATED_RESOURCES),
    )
