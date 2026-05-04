"""Provider selector — picks top STT/TTS/LLM/S2S candidates from bundled v0 fixtures.

This module is the Python counterpart to
``packages/core/src/lib/services/provider-selector.ts``. The TypeScript
runtime selector reads live ``ProviderScore`` rows from the policy
store; here we read the per-language ``speko.routing.{stt,tts,s2s,llm}``
fixtures shipped alongside the package and replay the same composite
math against the precomputed normalized axes.

Why two implementations? The TS selector serves the production
``/v1/sessions`` hot path — it has access to live benchmark deltas and
operator-tunable weights. The MCP server has neither. It needs a
deterministic, dependency-free read-path that can answer "what would
SpekoAI route to today?" using only the data shipped in the wheel.

Mode resolution mirrors the TS selector exactly:

* STT/TTS — ``region == 'global'`` selects the ``"batch"`` mode key;
  any other region selects ``f"streaming.{region}"``. Providers without
  that mode key fall out.
* S2S — realtime-only. ``"global"`` falls back to ``"realtime.us-east4"``
  and emits a ``notes`` entry so callers know the fallback happened.
* LLM — language-only filter; all rows live at one virtual mode.

For ``optimize_for == "balanced"`` we trust the fixture's precomputed
``composite``. For any other preset we recompute the composite from the
per-axis ``*_norm`` values using the per-type preset weight tables
mirrored from the TS file (lines 90-116). LLM is special — its rows
have no precomputed norms, so we compute min-max-inverted normalization
at query time over the active candidate set.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal

from pydantic import BaseModel, Field

OptimizeFor = Literal["latency", "accuracy", "cost"]
RequestType = Literal["stt", "llm", "tts", "s2s"]


# ---------------------------------------------------------------------------
# Per-type preset weight tables. v0 ships three optimization axes —
# latency, accuracy, cost — mirrored verbatim from the TS provider-
# selector's same-named columns. We dropped "balanced" because every
# axis already tilts a vector that's at least a little balanced; users
# pick the dimension they want to win on. We dropped "speed" because
# it overlaps with latency in voice-AI usage. The bundled benchmarks
# are not yet vertical-tuned, so vertical / use-case branching stays
# off until the data justifies it.
# ---------------------------------------------------------------------------

_STT_PRESETS: dict[OptimizeFor, dict[str, float]] = {
    "latency":  {"wer": 0.2,  "ttfp": 0.7,  "cost": 0.1},
    "accuracy": {"wer": 0.8,  "ttfp": 0.15, "cost": 0.05},
    "cost":     {"wer": 0.15, "ttfp": 0.15, "cost": 0.7},
}

_TTS_PRESETS: dict[OptimizeFor, dict[str, float]] = {
    "latency":  {"cer": 0.2,  "ttfb": 0.7,  "cost": 0.1},
    "accuracy": {"cer": 0.8,  "ttfb": 0.15, "cost": 0.05},
    "cost":     {"cer": 0.15, "ttfb": 0.15, "cost": 0.7},
}

_S2S_PRESETS: dict[OptimizeFor, dict[str, float]] = {
    "latency":  {"tool_call_p50": 0.7,  "task_success": 0.2,  "cost": 0.1},
    "accuracy": {"tool_call_p50": 0.15, "task_success": 0.8,  "cost": 0.05},
    "cost":     {"tool_call_p50": 0.15, "task_success": 0.15, "cost": 0.7},
}

_LLM_PRESETS: dict[OptimizeFor, dict[str, float]] = {
    "latency":  {"quality": 0.2,  "ttft": 0.7,  "cost": 0.1},
    "accuracy": {"quality": 0.8,  "ttft": 0.15, "cost": 0.05},
    "cost":     {"quality": 0.15, "ttft": 0.15, "cost": 0.7},
}


# Provider families the SpekoAI runtime can actually dispatch to today.
# The ``supported`` flag on a candidate flips on when its family ID
# (derived from the fixture row's ``id`` prefix split on first ``-``)
# is in the relevant set. Rows with families outside this set still
# appear in the leaderboard so callers can see where the benchmark
# data points to — they just need to wait for the runtime to catch up.
SUPPORTED: dict[RequestType, frozenset[str]] = {
    "stt": frozenset({"deepgram", "assemblyai", "openai", "elevenlabs", "xai"}),
    "llm": frozenset({"openai", "xai", "anthropic", "google"}),
    "tts": frozenset({"elevenlabs", "cartesia", "openai", "xai"}),
    "s2s": frozenset({"openai", "google", "xai"}),
}


# Bundled v0 fixture filenames. Loaded lazily through importlib.resources
# so the wheel's ``_data/`` directory is the single source of truth.
_FIXTURES: dict[str, str] = {
    "stt": "stt-routing-v0.json",
    "tts": "tts-routing-v0.json",
    "s2s": "s2s-routing-v0.json",
    "llm": "llm-routing-v0.json",
    "seed": "seed-dev.json",
}


class RankedCandidate(BaseModel):
    """One ranked provider pick the MCP tool surfaces to its caller."""

    provider_id: str = Field(
        description="Unique fixture row id, e.g. 'deepgram-nova3'."
    )
    canonical_id: str = Field(
        description=(
            "Provider family id derived from the fixture's id prefix "
            "(split on first '-'). Drives the runtime supported flag."
        ),
    )
    display_name: str = Field(
        description="Human label, e.g. 'Deepgram Nova-3'."
    )
    model_id: str = Field(description="Model name, e.g. 'Nova-3'.")
    score: float = Field(
        description=(
            "Composite score in [0, 1]. For optimize_for='balanced' "
            "this is the fixture's precomputed composite; otherwise "
            "it is recomputed from per-axis norms using the preset "
            "weights for the requested optimize_for."
        ),
    )
    primary_latency_ms: int | None = Field(
        default=None,
        description=(
            "Headline latency for this request type: "
            "STT streaming -> TTFP p50 short clip; STT batch -> batch "
            "latency p50; TTS streaming -> TTFB p50 by region; TTS "
            "batch -> batch TTFB; S2S -> tool_call p50; LLM -> TTFT p50. "
            "Null when the fixture omits the field."
        ),
    )
    cost_per_min_usd: float | None = Field(
        default=None,
        description=(
            "Per-minute cost in USD. Null when the provider's pricing "
            "is unpublished (e.g. xAI realtime today)."
        ),
    )
    status: str = Field(
        description="Fixture status: 'production' | 'provisional' | 'warned'."
    )
    supported: bool = Field(
        description=(
            "True when the SpekoAI runtime can dispatch to this "
            "provider family today (canonical_id in SUPPORTED[type])."
        ),
    )
    mode: str = Field(
        description=(
            "Mode key the score corresponds to: 'batch' | "
            "'streaming.<region>' | 'realtime.<region>' | 'streaming_sse' "
            "(LLM)."
        ),
    )


class SelectionResult(BaseModel):
    """Top-N picks across all four modalities for one (language, region, optimize_for)."""

    intent: dict
    stt: list[RankedCandidate]
    llm: list[RankedCandidate]
    tts: list[RankedCandidate]
    s2s: list[RankedCandidate]
    data_generated_at: str = Field(
        description=(
            "Earliest 'generated_at' across the four bundled v0 "
            "fixtures (ISO date). Lets callers cite a freshness "
            "watermark without parsing each fixture."
        ),
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable advisories — language-not-covered, S2S "
            "region fallback, etc. Empty when nothing notable happened."
        ),
    )


# ---------------------------------------------------------------------------
# Fixture loading helpers
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _load_fixture(name: str) -> dict[str, Any]:
    """Load a bundled v0 fixture from ``spekoai_mcp/_data/``."""
    raw = (files("spekoai_mcp._data") / _FIXTURES[name]).read_text(
        encoding="utf-8"
    )
    return json.loads(raw)


def _earliest_generated_at() -> str:
    """Return the lex-min generated_at across the four routing fixtures.

    All v0 fixtures emit ISO date strings (YYYY-MM-DD), so lex-sort
    matches chronological sort.
    """
    dates: list[str] = []
    for key in ("stt", "tts", "s2s", "llm"):
        try:
            d = _load_fixture(key).get("generated_at")
        except Exception:  # noqa: BLE001 - any load failure -> skip the date
            d = None
        if isinstance(d, str):
            dates.append(d)
    if not dates:
        return ""
    return min(dates)


def _canonical_from_id(provider_row_id: str) -> str:
    """Derive the family id from a fixture row's id (split on first '-').

    The TTS fixture ships an explicit ``canonical_id`` field but it
    encodes the *model family* (``elevenlabs-v3``), not the company —
    so it doesn't line up with the runtime SUPPORTED set. The STT and
    S2S fixtures have no ``canonical_id`` at all. Splitting on the
    first dash collapses everything to the company prefix that
    SUPPORTED keys on (``elevenlabs``, ``deepgram``, ``openai``, ...).
    """
    return provider_row_id.split("-", 1)[0]


def _display_name(name: str, model: str) -> str:
    """Compose a human label, avoiding double-printing the model name."""
    if not model:
        return name
    if model in name:
        return name
    return f"{name} {model}"


def _stt_primary_latency(provider: dict[str, Any], mode_key: str) -> int | None:
    """Headline STT latency for the requested mode key."""
    eng = provider.get("english") or {}
    if mode_key == "batch":
        v = eng.get("batch_latency_p50_ms")
    elif mode_key.startswith("streaming."):
        region = mode_key.split(".", 1)[1]
        v = (eng.get("streaming_ttfp_p50_ms_short") or {}).get(region)
    else:
        v = None
    return int(v) if isinstance(v, (int, float)) else None


def _tts_primary_latency(provider: dict[str, Any], mode_key: str) -> int | None:
    """Headline TTS latency for the requested mode key."""
    eng = provider.get("english") or {}
    if mode_key == "batch":
        v = eng.get("batch_ttfb_p50_ms")
    elif mode_key.startswith("streaming."):
        region = mode_key.split(".", 1)[1]
        v = (eng.get("streaming_ttfb_p50_ms_by_region") or {}).get(region)
    else:
        v = None
    return int(v) if isinstance(v, (int, float)) else None


def _s2s_primary_latency(provider: dict[str, Any], mode_key: str) -> int | None:
    """Headline S2S latency: tool_call p50 for the requested region."""
    eng = provider.get("english") or {}
    if not mode_key.startswith("realtime."):
        return None
    region = mode_key.split(".", 1)[1]
    bucket = (eng.get("tool_call_turn") or {}).get(region)
    if not isinstance(bucket, dict):
        return None
    v = bucket.get("p50_ms")
    return int(v) if isinstance(v, (int, float)) else None


def _stt_tts_cost(provider: dict[str, Any]) -> float | None:
    """Pick the tier1k per-minute cost when available, else fall back."""
    cost = provider.get("cost_per_minute_usd")
    if isinstance(cost, dict):
        for key in ("tier1k", "value", "per_minute_usd"):
            v = cost.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    if isinstance(cost, (int, float)):
        return float(cost)
    return None


def _s2s_cost(provider: dict[str, Any]) -> float | None:
    """S2S cost: ``cost_per_minute_usd.value`` when published, else None."""
    cost = provider.get("cost_per_minute_usd")
    if isinstance(cost, dict):
        v = cost.get("value")
        if isinstance(v, (int, float)):
            return float(v)
    if isinstance(cost, (int, float)):
        return float(cost)
    return None


# ---------------------------------------------------------------------------
# Per-type rankers
# ---------------------------------------------------------------------------


def _rank_routing_v0(
    fixture_key: Literal["stt", "tts", "s2s"],
    mode_key: str,
    optimize_for: OptimizeFor,
    primary_latency_fn,
    cost_fn,
    axis_norm_keys: dict[str, str],
    preset_table: dict[OptimizeFor, dict[str, float]],
) -> list[RankedCandidate]:
    """Shared ranker for the three routing-v0 fixtures (precomputed norms).

    All three presets recompute composite from per-axis ``*_norm`` values
    using the preset weight table — the fixture's pre-baked composite is
    "balanced"-tuned and would be wrong for any of the three v0 presets.
    """
    fixture = _load_fixture(fixture_key)
    out: list[RankedCandidate] = []
    for prov in fixture.get("providers", []):
        if prov.get("status") == "warned":
            continue
        scores = (prov.get("scores") or {}).get(mode_key)
        if not isinstance(scores, dict):
            continue
        if not scores.get("passed_filter", False):
            continue

        weights = preset_table[optimize_for]
        composite = 0.0
        for axis, weight in weights.items():
            # Look up the axis's norm field; the fixture's batch mode
            # for STT swaps ``ttfp_norm`` for ``latency_norm``.
            norm_field = axis_norm_keys[axis]
            v = scores.get(norm_field)
            if v is None and axis == "ttfp" and mode_key == "batch":
                v = scores.get("latency_norm")
            if v is None:
                v = 0.5  # neutral fallback for missing axis
            composite += weight * float(v)

        prov_id = prov.get("id", "")
        canonical = _canonical_from_id(prov_id)
        out.append(
            RankedCandidate(
                provider_id=prov_id,
                canonical_id=canonical,
                display_name=_display_name(prov.get("name", ""), prov.get("model", "")),
                model_id=prov.get("model", ""),
                score=float(composite),
                primary_latency_ms=primary_latency_fn(prov, mode_key),
                cost_per_min_usd=cost_fn(prov),
                status=prov.get("status", "unknown"),
                supported=canonical in SUPPORTED[fixture_key],
                mode=mode_key,
            )
        )

    out.sort(
        key=lambda c: (-c.score, c.canonical_id, c.model_id),
    )
    return out


def _rank_stt(mode_key: str, optimize_for: OptimizeFor) -> list[RankedCandidate]:
    return _rank_routing_v0(
        fixture_key="stt",
        mode_key=mode_key,
        optimize_for=optimize_for,
        primary_latency_fn=_stt_primary_latency,
        cost_fn=_stt_tts_cost,
        # STT batch fixture writes 'latency_norm' instead of 'ttfp_norm';
        # _rank_routing_v0 has the fallback wired in.
        axis_norm_keys={"wer": "wer_norm", "ttfp": "ttfp_norm", "cost": "cost_norm"},
        preset_table=_STT_PRESETS,
    )


def _rank_tts(mode_key: str, optimize_for: OptimizeFor) -> list[RankedCandidate]:
    return _rank_routing_v0(
        fixture_key="tts",
        mode_key=mode_key,
        optimize_for=optimize_for,
        primary_latency_fn=_tts_primary_latency,
        cost_fn=_stt_tts_cost,
        axis_norm_keys={"cer": "cer_norm", "ttfb": "ttfb_norm", "cost": "cost_norm"},
        preset_table=_TTS_PRESETS,
    )


def _rank_s2s(mode_key: str, optimize_for: OptimizeFor) -> list[RankedCandidate]:
    return _rank_routing_v0(
        fixture_key="s2s",
        mode_key=mode_key,
        optimize_for=optimize_for,
        primary_latency_fn=_s2s_primary_latency,
        cost_fn=_s2s_cost,
        axis_norm_keys={
            "tool_call_p50": "tool_call_p50_norm",
            "task_success": "task_success_norm",
            "cost": "cost_norm",
        },
        preset_table=_S2S_PRESETS,
    )


# ---------------------------------------------------------------------------
# LLM ranker — fixture has no precomputed norms; we compute on the fly.
# ---------------------------------------------------------------------------


def _llm_rows(language: str) -> list[dict[str, Any]]:
    """Combine seed-dev AA rows + the curated llm-routing-v0 fixture.

    Dedupe by (provider_family, model_id). The curated fixture wins
    when both sources carry the same row; AA fills in entries the
    curated set hasn't covered yet.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    # Seed AA first (lower priority).
    seed = _load_fixture("seed")
    for r in seed.get("artificial_analysis", []) or []:
        if r.get("language") != language:
            continue
        canonical = r.get("provider_id", "")
        model = r.get("model_id", "")
        # AA rows: cost_per_min_usd computed from per-million prices
        # using a 150 input + 80 output tok/min conversational mix.
        per_min: float | None = None
        ip = r.get("price_per_m_input_usd")
        op = r.get("price_per_m_output_usd")
        if isinstance(ip, (int, float)) and isinstance(op, (int, float)):
            per_min = (150 / 1_000_000) * float(ip) + (80 / 1_000_000) * float(op)
        rows[(canonical, model)] = {
            "id": f"{canonical}-{model}",
            "canonical_id": canonical,
            "name": canonical.title(),
            "model": model,
            "language": r.get("language", "en"),
            "status": "production",
            "supports_tools": True,
            "quality_score": float(r.get("aa_index") or 0.0),
            "ttft_p50_ms": int(r.get("ttft_ms") or 0) or None,
            "cost_per_min_usd": per_min,
        }

    # Curated routing-v0 wins.
    curated = _load_fixture("llm")
    for r in curated.get("providers", []) or []:
        if r.get("language", "en") != language:
            continue
        canonical = r.get("canonical_id") or _canonical_from_id(r.get("id", ""))
        model = r.get("model", "")
        rows[(canonical, model)] = {
            "id": r.get("id", f"{canonical}-{model}"),
            "canonical_id": canonical,
            "name": r.get("name", canonical.title()),
            "model": model,
            "language": r.get("language", "en"),
            "status": r.get("status", "production"),
            "supports_tools": r.get("supports_tools", True),
            "quality_score": float(r.get("quality_score") or 0.0),
            "ttft_p50_ms": r.get("ttft_p50_ms"),
            "cost_per_min_usd": r.get("cost_per_min_usd"),
        }

    return list(rows.values())


def _rank_llm(language: str, optimize_for: OptimizeFor) -> list[RankedCandidate]:
    rows = _llm_rows(language)
    rows = [r for r in rows if r.get("status") != "warned"]
    if not rows:
        return []

    # Min-max-inverted normalization over the active candidate set —
    # mirrors the TS computeNorms helper exactly.
    def axis_values(reader, direction: Literal["lower", "higher"]) -> list[float]:
        vals: list[float] = []
        for r in rows:
            v = reader(r)
            vals.append(0.5 if v is None else float(v))
        lo = min(vals)
        hi = max(vals)
        span = hi - lo
        if span == 0:
            return [1.0 for _ in vals]
        return [
            ((hi - v) / span) if direction == "lower" else ((v - lo) / span)
            for v in vals
        ]

    quality_norm = axis_values(lambda r: r.get("quality_score"), "higher")
    ttft_norm = axis_values(lambda r: r.get("ttft_p50_ms"), "lower")
    cost_norm = axis_values(lambda r: r.get("cost_per_min_usd"), "lower")

    weights = _LLM_PRESETS[optimize_for]
    out: list[RankedCandidate] = []
    for i, r in enumerate(rows):
        composite = (
            weights["quality"] * quality_norm[i]
            + weights["ttft"] * ttft_norm[i]
            + weights["cost"] * cost_norm[i]
        )
        canonical = r["canonical_id"]
        out.append(
            RankedCandidate(
                provider_id=r["id"],
                canonical_id=canonical,
                display_name=_display_name(r["name"], r["model"]),
                model_id=r["model"],
                score=float(composite),
                primary_latency_ms=(
                    int(r["ttft_p50_ms"])
                    if isinstance(r.get("ttft_p50_ms"), (int, float))
                    else None
                ),
                cost_per_min_usd=(
                    float(r["cost_per_min_usd"])
                    if isinstance(r.get("cost_per_min_usd"), (int, float))
                    else None
                ),
                status=r.get("status", "production"),
                supported=canonical in SUPPORTED["llm"],
                mode="streaming_sse",
            )
        )

    out.sort(key=lambda c: (-c.score, c.canonical_id, c.model_id))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _dedupe_and_cap(
    candidates: list[RankedCandidate], limit: int
) -> list[RankedCandidate]:
    """Collapse same (canonical_id, model_id) duplicates, keep best score, cap."""
    seen: dict[tuple[str, str], RankedCandidate] = {}
    for c in candidates:
        key = (c.canonical_id, c.model_id)
        prev = seen.get(key)
        if prev is None or c.score > prev.score:
            seen[key] = c
    out = list(seen.values())
    out.sort(key=lambda c: (-c.score, c.canonical_id, c.model_id))
    return out[:limit]


def select_ranked(
    language: str = "en",
    region: str = "global",
    optimize_for: OptimizeFor = "latency",
    limit: int = 3,
) -> SelectionResult:
    """Return the top-``limit`` STT/TTS/LLM/S2S picks for a routing intent.

    Mode resolution mirrors the TS provider-selector:

    * STT/TTS — ``region == 'global'`` => batch; else streaming.<region>.
      Providers without that mode key are skipped.
    * S2S — realtime-only. ``'global'`` falls back to
      ``realtime.us-east4`` and emits a notes entry.
    * LLM — language-only; one virtual mode (``streaming_sse``).

    Hard filters: skip ``status == 'warned'``; skip rows with
    ``passed_filter is False`` for the chosen mode key. Composite is
    always recomputed from the per-axis ``*_norm`` values using the
    preset weights for the requested ``optimize_for`` (latency / accuracy
    / cost). Default axis is ``latency`` — the dimension voice-AI users
    feel first.

    Language scope today is English-only — every v0 fixture's
    ``language_scope`` is ``["en"]``. A non-en request returns empty
    modality lists plus a notes entry citing the gap.
    """
    notes: list[str] = []

    # Resolve modes per modality.
    stt_mode = "batch" if region == "global" else f"streaming.{region}"
    tts_mode = "batch" if region == "global" else f"streaming.{region}"
    if region == "global":
        s2s_mode = "realtime.us-east4"
        notes.append(
            "S2S has no batch mode; 'global' falls back to realtime.us-east4 — "
            "set region explicitly for non-US callers."
        )
    else:
        s2s_mode = f"realtime.{region}"

    # Language scope filter — v0 fixtures cover English only.
    if language != "en":
        notes.append(
            f"v0 fixtures cover language=en only; STT/TTS/LLM/S2S "
            f"picks unavailable for language={language}."
        )
        return SelectionResult(
            intent={
                "language": language,
                "region": region,
                "optimize_for": optimize_for,
            },
            stt=[],
            llm=[],
            tts=[],
            s2s=[],
            data_generated_at=_earliest_generated_at(),
            notes=notes,
        )

    stt = _dedupe_and_cap(_rank_stt(stt_mode, optimize_for), limit)
    tts = _dedupe_and_cap(_rank_tts(tts_mode, optimize_for), limit)
    s2s = _dedupe_and_cap(_rank_s2s(s2s_mode, optimize_for), limit)
    llm = _dedupe_and_cap(_rank_llm(language, optimize_for), limit)

    return SelectionResult(
        intent={
            "language": language,
            "region": region,
            "optimize_for": optimize_for,
        },
        stt=stt,
        llm=llm,
        tts=tts,
        s2s=s2s,
        data_generated_at=_earliest_generated_at(),
        notes=notes,
    )


__all__ = [
    "OptimizeFor",
    "RankedCandidate",
    "RequestType",
    "SUPPORTED",
    "SelectionResult",
    "select_ranked",
]
