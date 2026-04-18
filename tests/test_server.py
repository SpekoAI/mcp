"""Tests for `spekoai_mcp.server`.

Covers server construction and the OAuth-forwarding behaviour of tools:
every call must mint an SDK client with the caller's bearer token, and
calls with no token must raise `ToolError` rather than silently hit the
upstream API with no credentials.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken

from spekoai_mcp.server import create_server


@pytest.fixture
def sample_summary() -> dict[str, Any]:
    # Minimal shape accepted by spekoai.models.UsageSummary — patched SDK
    # returns it verbatim so the test doesn't have to import/construct the
    # model in two places.
    return {"total_cost": 0, "period_start": "2026-04-01", "period_end": "2026-04-30"}


async def test_create_server_without_auth() -> None:
    mcp = create_server()
    # Tool registry is the contract we care about — the one declared tool
    # must be reachable by name.
    tools = await mcp.list_tools()
    assert any(t.name == "get_usage_summary" for t in tools)


async def test_tool_rejects_missing_token() -> None:
    """With no OAuth token on the request, the tool refuses to proceed."""
    mcp = create_server()

    with patch("spekoai_mcp.server.get_access_token", return_value=None):
        with pytest.raises(ToolError, match="OAuth access token"):
            await mcp._call_tool_mcp("get_usage_summary", {})  # type: ignore[attr-defined]


async def test_tool_forwards_caller_token(sample_summary: dict[str, Any]) -> None:
    """The SDK client is constructed with the caller's token, not a shared key."""
    mcp = create_server()
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, *, api_key: str, base_url: str) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.usage = AsyncMock()
            self.usage.get = AsyncMock(return_value=sample_summary)

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    token = AccessToken(token="caller-jwt", client_id="cid", scopes=[])

    with (
        patch("spekoai_mcp.server.get_access_token", return_value=token),
        patch("spekoai_mcp.server.AsyncSpeko", _FakeClient),
    ):
        await mcp._call_tool_mcp(  # type: ignore[attr-defined]
            "get_usage_summary", {"from_date": "2026-04-01"}
        )

    assert captured["api_key"] == "caller-jwt"
    assert captured["base_url"] == "https://api.speko.ai"
