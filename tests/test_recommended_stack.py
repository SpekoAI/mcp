"""Tests for `recommended_stack` — the vertical → stack decision tool.

Coverage: every one of the four Speko verticals returns a populated
`StackRecommendation` with the expected tagline, @spekoai packages,
at least one warning, and the `next_tool` handoff pointing at
`scaffold_voice_app`. Pydantic validation rejects unknown use cases
before our code runs.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from spekoai_mcp.recommendations import StackRecommendation, recommend
from spekoai_mcp.server import create_server

_VERTICALS = ["general", "healthcare", "finance", "legal"]


@pytest.mark.parametrize("use_case", _VERTICALS)
def test_recommend_returns_populated_stack(use_case: str) -> None:
    rec = recommend(use_case)  # type: ignore[arg-type]
    assert isinstance(rec, StackRecommendation)
    assert rec.use_case == use_case
    assert rec.tagline  # non-empty
    assert rec.rationale
    assert rec.warnings, f"{use_case} has no warnings"
    assert rec.next_tool == "scaffold_voice_app"
    names = {p.name for p in rec.packages}
    assert "@spekoai/client" in names
    assert "@spekoai/sdk" in names


def test_healthcare_tagline_matches_speko_dev() -> None:
    rec = recommend("healthcare")
    assert "Clinical-grade accuracy" in rec.tagline
    joined = " ".join(rec.warnings)
    assert "HIPAA" in joined


def test_legal_warns_about_privileged_communications() -> None:
    rec = recommend("legal")
    joined = " ".join(rec.warnings).lower()
    assert "privileged" in joined or "attorney" in joined


def test_finance_warns_about_identity_verification() -> None:
    rec = recommend("finance")
    joined = " ".join(rec.warnings)
    assert "identity" in joined.lower()


def test_general_has_a_baseline_warning() -> None:
    rec = recommend("general")
    # General has no compliance regime baked in; surface that to callers
    # so they can't assume the scaffold protects them.
    joined = " ".join(rec.warnings).lower()
    assert "vertical" in joined or "audit" in joined


async def test_recommended_stack_tool_advertised() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    assert any(t.name == "recommended_stack" for t in tools)


async def test_recommended_stack_tool_returns_healthcare_payload() -> None:
    mcp = create_server()
    result = await mcp.call_tool(
        "recommended_stack", {"use_case": "healthcare"}
    )
    payload = result.structured_content or {}
    assert payload.get("use_case") == "healthcare"
    assert payload.get("next_tool") == "scaffold_voice_app"
    package_names = {p["name"] for p in payload.get("packages", [])}
    assert "@spekoai/client" in package_names


async def test_recommended_stack_tool_rejects_unknown_use_case() -> None:
    mcp = create_server()
    with pytest.raises((ValidationError, Exception)):
        await mcp.call_tool("recommended_stack", {"use_case": "insurance"})
