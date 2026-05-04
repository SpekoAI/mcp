"""Tests for `recommended_stack` — provider picks + Speko package set.

Coverage: the tool returns the two `@spekoai/*` packages plus real
top-3 STT/TTS/LLM/S2S picks for each `optimize_for` axis (latency,
accuracy, cost), echoes the routing intent, and surfaces a
`data_generated_at` watermark + selection notes.

Vertical / use-case branching is deliberately not exposed in v0 —
benchmark data isn't tuned per vertical yet, so passing a vertical
would only let us lie with confidence. Tests below assert that's the
case (no `use_case` parameter survives) and that the three exposed
optimize axes produce meaningfully different rankings.
"""

from __future__ import annotations

import pytest

from spekoai_mcp.recommendations import StackRecommendation, recommend
from spekoai_mcp.server import create_server

_OPTIMIZE_AXES = ["latency", "accuracy", "cost"]


@pytest.mark.parametrize("optimize_for", _OPTIMIZE_AXES)
def test_recommend_returns_populated_stack(optimize_for: str) -> None:
    rec = recommend(optimize_for=optimize_for)  # type: ignore[arg-type]
    assert isinstance(rec, StackRecommendation)
    assert rec.optimize_for == optimize_for
    assert rec.summary  # non-empty
    names = {p.name for p in rec.packages}
    assert "@spekoai/client" in names
    assert "@spekoai/sdk" in names
    assert rec.next_tool == "scaffold_voice_app"


def test_default_optimize_for_is_latency() -> None:
    rec = recommend()
    assert rec.optimize_for == "latency"
    assert rec.intent.get("optimize_for") == "latency"


def test_summary_changes_per_optimize_axis() -> None:
    summaries = {axis: recommend(optimize_for=axis).summary for axis in _OPTIMIZE_AXES}
    # All three summaries must be distinct so the agent can tell them apart.
    assert len(set(summaries.values())) == 3


def test_picks_present_for_default_intent() -> None:
    rec = recommend(language="en", region="us-east4")
    assert rec.stt, "STT picks empty for en/us-east4/latency"
    assert rec.tts, "TTS picks empty for en/us-east4/latency"
    assert rec.s2s, "S2S picks empty for en/us-east4/latency"
    assert rec.llm, "LLM picks empty for en"
    for picks in (rec.stt, rec.tts, rec.s2s, rec.llm):
        assert len(picks) <= 3
        for c in picks:
            assert c.provider_id, "every pick has a provider_id"
            assert 0.0 < c.score <= 1.0, f"score out of (0, 1]: {c.score}"


def test_latency_optimized_stt_prefers_lower_ttfp() -> None:
    rec = recommend(optimize_for="latency", language="en", region="us-east4")
    assert len(rec.stt) >= 2, "need at least 2 STT picks for the comparison"
    top, runner_up = rec.stt[0], rec.stt[1]
    assert top.primary_latency_ms is not None
    assert runner_up.primary_latency_ms is not None
    assert top.primary_latency_ms <= runner_up.primary_latency_ms, (
        f"latency-optimized top STT ({top.provider_id} "
        f"{top.primary_latency_ms}ms) should not have higher TTFP than "
        f"runner-up ({runner_up.provider_id} {runner_up.primary_latency_ms}ms)"
    )


def test_unsupported_language_returns_empty_with_note() -> None:
    rec = recommend(language="es")
    assert rec.stt == []
    assert rec.tts == []
    assert rec.s2s == []
    assert rec.llm == []
    joined = " ".join(rec.notes)
    assert "language=es" in joined
    # Packages still ship — they're language-independent.
    assert {p.name for p in rec.packages} >= {"@spekoai/client", "@spekoai/sdk"}


def test_data_generated_at_present() -> None:
    rec = recommend()
    assert rec.data_generated_at, "data_generated_at must be a non-empty ISO date"
    assert len(rec.data_generated_at) >= 10
    assert rec.data_generated_at[4] == "-"
    assert rec.data_generated_at[7] == "-"


def test_intent_echo_round_trips() -> None:
    rec = recommend(optimize_for="accuracy", language="en", region="europe-west3")
    assert rec.intent == {
        "language": "en",
        "region": "europe-west3",
        "optimize_for": "accuracy",
    }


async def test_recommended_stack_tool_advertised() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    assert any(t.name == "recommended_stack" for t in tools)


async def test_recommended_stack_tool_default_payload() -> None:
    mcp = create_server()
    result = await mcp.call_tool("recommended_stack", {})
    payload = result.structured_content or {}
    assert payload.get("optimize_for") == "latency"
    assert payload.get("next_tool") == "scaffold_voice_app"
    package_names = {p["name"] for p in payload.get("packages", [])}
    assert "@spekoai/client" in package_names


async def test_recommended_stack_tool_round_trip_new_params() -> None:
    mcp = create_server()
    result = await mcp.call_tool(
        "recommended_stack",
        {
            "optimize_for": "latency",
            "language": "en",
            "region": "us-east4",
        },
    )
    payload = result.structured_content or {}
    assert payload.get("optimize_for") == "latency"
    assert payload.get("intent", {}).get("region") == "us-east4"
    assert payload.get("data_generated_at")
    assert payload.get("stt"), "STT picks must be non-empty for en/us-east4"
    assert payload.get("llm"), "LLM picks must be non-empty"
    for c in payload["stt"]:
        assert "provider_id" in c
        assert "primary_latency_ms" in c
        assert "supported" in c


async def test_recommended_stack_tool_rejects_unknown_optimize_for() -> None:
    mcp = create_server()
    with pytest.raises(Exception):
        await mcp.call_tool("recommended_stack", {"optimize_for": "vibes"})


async def test_recommended_stack_tool_does_not_accept_use_case() -> None:
    """Vertical branching was deliberately removed in v0 — the tool
    should not advertise a `use_case` parameter."""
    mcp = create_server()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "recommended_stack")
    schema_props = (tool.parameters or {}).get("properties", {})
    assert "use_case" not in schema_props, (
        "recommended_stack must not expose `use_case` until benchmark "
        "data is vertical-tuned"
    )
    # Three v0 axes only — confirm the closed enumeration.
    assert set(schema_props.get("optimize_for", {}).get("enum", [])) == {
        "latency",
        "accuracy",
        "cost",
    }
