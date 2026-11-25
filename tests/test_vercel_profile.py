"""Tests for the Vercel Connect host's deployment-bound tool profile.

Vercel Connect is a published directory, so the AI-disclosure policy applies.
The host serves the Gemini surface (calls first); test_gemini_profile.py covers
what that surface may and may not contain.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import NotFoundError

from spekoai_mcp.profiles import (
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
    GEMINI_PROFILE,
    KNOWN_PROFILES,
    VERCEL_PROFILE,
    profile_serves_phone_calls,
)
from spekoai_mcp.server import create_server


async def _served_names(monkeypatch: pytest.MonkeyPatch, profile: str) -> list[str]:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    return [tool.name for tool in await create_server().list_tools()]


def test_vercel_is_a_known_directory_profile_that_places_calls() -> None:
    # Directory membership is what forces the AI disclosure opener.
    assert VERCEL_PROFILE in KNOWN_PROFILES
    assert VERCEL_PROFILE in DIRECTORY_PROFILES
    assert profile_serves_phone_calls(VERCEL_PROFILE)


async def test_vercel_serves_the_gemini_surface_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _served_names(monkeypatch, VERCEL_PROFILE) == await _served_names(
        monkeypatch, GEMINI_PROFILE
    )


async def test_vercel_refuses_tools_outside_the_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, VERCEL_PROFILE)
    server = create_server()
    for name in ("agents.create", "agents.deploy", "api_keys.create"):
        with pytest.raises(NotFoundError):
            await server.call_tool(name, {})
