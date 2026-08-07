"""Tests for `spekoai_mcp.selector` — the v0 fixture-driven ranker.

These tests exercise the selector directly so we can guard the parts
of the contract `recommend(...)` and `scaffold_voice_app(...)` rely
on: warning-status filtering, optimize-for re-ranking, the S2S global
fallback, and the supported-flag mapping to the runtime SUPPORTED set.
"""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from spekoai_mcp.selector import SUPPORTED, OptimizeFor, select_ranked


def test_select_latency_returns_top_3_stt() -> None:
    sel = select_ranked(language="en", region="global", optimize_for="latency", limit=3)
    assert len(sel.stt) <= 3
    assert sel.stt, "STT picks empty for en/global/latency"
    # Score is non-increasing.
    for a, b in zip(sel.stt, sel.stt[1:]):
        assert a.score >= b.score, (
            f"STT picks not score-descending: {a.provider_id} "
            f"({a.score}) < {b.provider_id} ({b.score})"
        )


@pytest.mark.parametrize("optimize_for", ["latency", "accuracy", "cost"])
def test_elevenlabs_mode_specific_scores_are_recommended(
    optimize_for: OptimizeFor,
) -> None:
    batch = select_ranked(language="en", region="global", optimize_for=optimize_for, limit=20)
    batch_ids = {candidate.provider_id for candidate in batch.stt}
    assert "elevenlabs-scribe-v2" not in batch_ids
    assert "elevenlabs-scribe-v1" in batch_ids

    streaming = select_ranked(language="en", region="us-east4", optimize_for=optimize_for, limit=20)
    streaming_ids = {candidate.provider_id for candidate in streaming.stt}
    assert "elevenlabs-scribe-v2" in streaming_ids
    assert "elevenlabs-scribe-v1" not in streaming_ids


def test_elevenlabs_fixture_keeps_realtime_and_batch_evidence_separate() -> None:
    raw = (files("spekoai_mcp._data") / "stt-routing-v0.json").read_text(encoding="utf-8")
    fixture = json.loads(raw)
    providers = {provider["id"]: provider for provider in fixture["providers"]}

    realtime = providers["elevenlabs-scribe-v2"]
    assert realtime["modes_supported"] == ["streaming"]
    assert "batch" not in realtime["protocols"]
    assert "batch" not in realtime["endpoints"]
    assert "batch_latency_p50_ms" not in realtime["english"]
    assert "batch" not in realtime["scores"]

    batch_v1 = providers["elevenlabs-scribe-v1"]
    assert batch_v1["english"]["wer_pct"] == 5.4
    assert batch_v1["english"]["batch_latency_p50_ms"] == 1019
    assert batch_v1["noise_robustness_avg_delta_pp"] == 1.5
    assert batch_v1["scores"]["batch"]["passed_filter"] is True

    ranked_ids = {row["id"] for row in fixture["rankings"]["batch"]["ranked"]}
    assert "elevenlabs-scribe-v2" not in ranked_ids
    assert "elevenlabs-scribe-v1" in ranked_ids


def test_warned_providers_excluded() -> None:
    sel = select_ranked(language="en", region="us-east4", optimize_for="latency", limit=10)
    for picks in (sel.stt, sel.tts, sel.s2s, sel.llm):
        for c in picks:
            assert c.status != "warned", (
                f"{c.provider_id} status=warned should have been filtered out"
            )


def test_optimize_for_latency_reranks_vs_accuracy() -> None:
    accuracy = select_ranked(language="en", region="us-east4", optimize_for="accuracy", limit=5)
    latency = select_ranked(language="en", region="us-east4", optimize_for="latency", limit=5)
    assert accuracy.stt and latency.stt
    acc_top = accuracy.stt[0]
    lat_top = latency.stt[0]
    # Either the top differs OR the latency-top has lower-or-equal
    # primary_latency_ms than the accuracy-top — both are evidence
    # the preset re-weighting is taking effect.
    if acc_top.provider_id == lat_top.provider_id:
        assert acc_top.primary_latency_ms is not None
        assert lat_top.primary_latency_ms is not None
        assert lat_top.primary_latency_ms <= acc_top.primary_latency_ms


def test_s2s_global_falls_back_to_us_east4_with_note() -> None:
    sel = select_ranked(language="en", region="global", optimize_for="latency", limit=3)
    assert sel.s2s, "S2S picks must be non-empty after global -> us-east4 fallback"
    joined = " ".join(sel.notes)
    assert "us-east4" in joined, (
        "global S2S queries should emit a notes entry mentioning "
        f"us-east4 fallback; got notes={sel.notes!r}"
    )
    # All S2S picks should have realtime.us-east4 mode.
    for c in sel.s2s:
        assert c.mode == "realtime.us-east4"


def test_supported_flag_matches_runtime_set() -> None:
    sel = select_ranked(language="en", region="us-east4", optimize_for="latency", limit=10)
    for type_key, picks in (
        ("stt", sel.stt),
        ("tts", sel.tts),
        ("s2s", sel.s2s),
        ("llm", sel.llm),
    ):
        allowed = SUPPORTED[type_key]
        for c in picks:
            assert c.supported == (c.canonical_id in allowed), (
                f"supported flag mismatch: {c.provider_id} canonical={c.canonical_id} "
                f"supported={c.supported} but allowed={c.canonical_id in allowed}"
            )


def test_data_generated_at_is_iso_date() -> None:
    sel = select_ranked(language="en", region="global", optimize_for="latency")
    assert sel.data_generated_at
    # YYYY-MM-DD prefix
    assert len(sel.data_generated_at) >= 10
    assert sel.data_generated_at[4] == "-"
    assert sel.data_generated_at[7] == "-"


def test_unsupported_language_returns_empty_with_notes() -> None:
    sel = select_ranked(language="ja", region="global", optimize_for="latency")
    assert sel.stt == []
    assert sel.tts == []
    assert sel.s2s == []
    assert sel.llm == []
    joined = " ".join(sel.notes)
    assert "language=ja" in joined


def test_llm_picks_present_for_english() -> None:
    sel = select_ranked(language="en", region="us-east4", optimize_for="latency", limit=3)
    assert sel.llm, "LLM picks must be non-empty for en"
    # Curated rows should beat AA-only rows (non-empty supported flag
    # comes from canonical_id being in SUPPORTED["llm"]).
    for c in sel.llm:
        assert c.canonical_id in SUPPORTED["llm"], (
            f"LLM pick {c.provider_id} canonical_id={c.canonical_id} not in SUPPORTED"
        )


def test_cost_optimize_prefers_cheaper_llm() -> None:
    cost_sel = select_ranked(language="en", region="us-east4", optimize_for="cost", limit=3)
    assert cost_sel.llm
    # The cost preset weights price-per-minute at 0.7. The top pick
    # should be at the cheap end of the curated LLM set. We don't
    # hard-code a specific provider — just assert the cost is in the
    # bottom half of the published distribution.
    top = cost_sel.llm[0]
    assert top.cost_per_min_usd is not None
    assert top.cost_per_min_usd < 0.005, (
        f"cost-optimized top LLM should be sub-$0.005/min; "
        f"got {top.provider_id} at {top.cost_per_min_usd}"
    )
