"""Opinionated stack recommendations for the four SpekoAI verticals.

The `recommended_stack` MCP tool wraps `recommend(use_case)` unchanged —
the rules-based logic here stays importable from tests without spinning
up a FastMCP server.

The four verticals mirror `VerticalSchema` in `@spekoai/core` — the API's
`/v1/sessions` endpoint rejects any other value. All four target the same
implementation stack today (Next.js App Router, Node runtime,
`@spekoai/client` in the browser, `@spekoai/sdk` on the backend). The
per-vertical surface is the tagline, rationale, and the compliance /
operational warnings an agent needs to surface to the user before
shipping a production build.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

UseCase = Literal[
    "general",
    "healthcare",
    "finance",
    "legal",
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
    "general": "A baseline voice agent — start here if your domain isn't vertical-specific.",
    "healthcare": "Clinical-grade accuracy — 98.5% medical-term accuracy.",
    "finance": "Audit-grade recording for regulated conversations.",
    "legal": "Evidence-grade transcripts for client intake and matter discovery.",
}

_RATIONALES: dict[UseCase, str] = {
    "general": (
        "A starting-point voice agent for domains we don't route with a "
        "specialist STT/LLM/TTS yet. @spekoai/client streams to Speko "
        "over WebRTC; the router picks default providers balanced for "
        "latency and cost. Swap the system prompt to fit your persona "
        "and upgrade to a vertical preset when one matches."
    ),
    "healthcare": (
        "Browser-based voice intake where mishearing a dosage or drug name "
        "is a safety event. @spekoai/client streams to Speko over WebRTC; "
        "the router prefers STT providers benchmarked on medical "
        "terminology. The backend mints short-lived conversation tokens "
        "so PHI never leaves your control plane."
    ),
    "finance": (
        "Customer-facing voice interactions that may be audited later. "
        "@spekoai/client handles the real-time conversation; the Speko "
        "API stores conversation metadata you can query for reporting. "
        "Identity verification is out-of-band — the voice agent must not "
        "be the sole auth factor."
    ),
    "legal": (
        "Client intake and matter discovery where the transcript is the "
        "record of record. @spekoai/client captures the conversation; "
        "the server-side SDK can re-run transcription on the stored "
        "audio for attorney review. The scaffold leaves the privileged-"
        "communication retention policy to you — see the warning below."
    ),
}

_WARNINGS: dict[UseCase, list[str]] = {
    "general": [
        "No vertical safeguards are baked in — audit the system prompt "
        "against your domain's specific do-not-do list before shipping.",
    ],
    "healthcare": [
        "Not HIPAA-compliant out of the box — sign a BAA with Speko and "
        "your own hosting vendor before routing PHI through this stack.",
        "STT confidence is not a diagnosis confidence. Have a clinician "
        "review any automated triage decisions.",
    ],
    "finance": [
        "Verify caller identity out-of-band (e.g. one-time code, known "
        "device) before discussing account details — the voice agent has "
        "no inherent authentication.",
        "Keep an immutable call log if your regulator requires it "
        "(FINRA 4511, MiFID II). The Speko API gives you the transcript; "
        "retention is your responsibility.",
    ],
    "legal": [
        "Transcripts of attorney-client conversations are privileged — "
        "store them with the same access controls as matter files, not "
        "in shared analytics buckets.",
        "Disclose recording at the start of the call per state-specific "
        "two-party-consent laws (e.g. CA, FL, IL, WA) before intake.",
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
