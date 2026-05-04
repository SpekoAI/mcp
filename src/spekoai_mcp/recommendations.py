"""Stack recommendations driven by real benchmark data.

The `recommended_stack` MCP tool wraps `recommend(...)` — kept import-
able from tests without a FastMCP server.

What this module returns: the two `@spekoai/*` packages everyone needs
plus the top-3 STT / LLM / TTS / S2S provider picks for the caller's
optimize-for axis (`latency` | `accuracy` | `speed`), pulled from the
v0 routing fixtures shipped in `_data/`. Vertical / use-case branching
was deliberately removed in this revision: until the benchmark data is
tuned per vertical, picking providers based on the vertical name would
just be lying with a confident voice. We surface the picks honestly
and let the caller pair them with their own domain prompt.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from spekoai_mcp.selector import (
    OptimizeFor,
    RankedCandidate,
    select_ranked,
)


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
    """SpekoAI stack + real provider picks for one routing intent."""

    optimize_for: OptimizeFor = Field(
        description=(
            "Optimization preset that drove the provider ranking: "
            "`latency` (minimize first-response time — default), "
            "`accuracy` (maximize quality scores), or `cost` "
            "(minimize per-minute price for high-volume apps)."
        ),
    )
    intent: dict = Field(
        default_factory=dict,
        description=(
            "Echo of the (language, region, optimize_for) intent that "
            "produced the picks."
        ),
    )
    summary: str = Field(
        description=(
            "One-paragraph plain-English summary of what these picks "
            "mean for the caller and how to read the four modality "
            "lists."
        ),
    )
    packages: list[PackageRecommendation] = Field(
        description="Speko packages to install regardless of routing picks.",
    )
    stt: list[RankedCandidate] = Field(
        default_factory=list,
        description="Top-3 STT candidates for the requested intent.",
    )
    llm: list[RankedCandidate] = Field(
        default_factory=list,
        description="Top-3 LLM candidates for the requested intent.",
    )
    tts: list[RankedCandidate] = Field(
        default_factory=list,
        description="Top-3 TTS candidates for the requested intent.",
    )
    s2s: list[RankedCandidate] = Field(
        default_factory=list,
        description="Top-3 speech-to-speech candidates for the requested intent.",
    )
    data_generated_at: str = Field(
        default="",
        description="Earliest 'generated_at' across the bundled v0 routing fixtures.",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="Selector advisories (language gaps, S2S region fallbacks, etc.).",
    )
    next_tool: str = Field(
        default="scaffold_voice_app",
        description="Suggested follow-up MCP tool to call with the same intent.",
    )
    related_resources: list[str] = Field(
        default_factory=list,
        description="spekoai://docs/... URIs the agent should read first.",
    )


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


_SUMMARIES: dict[OptimizeFor, str] = {
    "latency": (
        "Latency-optimized stack — providers ranked by p50 time-to-"
        "first-output (TTFP for STT, TTFB for TTS, TTFT for LLM, "
        "tool-call p50 for S2S). Default for voice — the perceived "
        "snappiness of the agent is usually the primary product signal."
    ),
    "accuracy": (
        "Accuracy-optimized stack — providers ranked primarily by "
        "quality scores (WER for STT, CER for TTS, AA index for LLM, "
        "task-success rate for S2S). Use when transcription / response "
        "fidelity matters more than first-response timing."
    ),
    "cost": (
        "Cost-optimized stack — providers ranked primarily by per-"
        "minute price. Useful for high-volume apps where margin "
        "dominates UX choices, or for early experimentation before "
        "you know which quality / latency floor your product needs."
    ),
}


def recommend(
    optimize_for: OptimizeFor = "latency",
    language: str = "en",
    region: str = "global",
) -> StackRecommendation:
    """Return the SpekoAI stack + real provider picks for one intent.

    Parameters

    - `optimize_for` — one of `latency` | `accuracy` | `cost`. Drives
      the per-axis weights used to rank STT/TTS/LLM/S2S picks.
      `latency` is the default; voice-AI users feel TTFB first.
    - `language` — caller's spoken language. v0 fixtures cover English
      only; non-English requests echo the intent and surface a notes
      entry but return empty modality lists.
    - `region` — `global` selects batch ranking for STT/TTS;
      `us-east4`, `europe-west3`, `asia-southeast1` select streaming /
      realtime ranking. S2S is realtime-only — `global` falls back to
      `realtime.us-east4` and we surface that fallback in `notes`.
    """
    selection = select_ranked(
        language=language, region=region, optimize_for=optimize_for, limit=3
    )

    return StackRecommendation(
        optimize_for=optimize_for,
        intent=selection.intent,
        summary=_SUMMARIES[optimize_for],
        packages=_default_packages(),
        stt=list(selection.stt),
        llm=list(selection.llm),
        tts=list(selection.tts),
        s2s=list(selection.s2s),
        data_generated_at=selection.data_generated_at,
        notes=list(selection.notes),
        related_resources=list(_RELATED_RESOURCES),
    )
