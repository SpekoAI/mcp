"""API-key sessions do not see or call actions the server refuses to API keys.

`api_keys.create` and the other credential, billing and agent-access actions
allow only an OAuth user. They used to be advertised to API-key sessions too,
so every call ended in 403 ACTION_PRINCIPAL_FORBIDDEN.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import NotFoundError

import spekoai_mcp.profiles as profiles
from spekoai_mcp.profiles import CUSTOMER_PROFILE, DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server


def _authenticate_as(monkeypatch: pytest.MonkeyPatch, auth_method: str | None) -> None:
    token = SimpleNamespace(claims={"auth_method": auth_method} if auth_method else {})
    monkeypatch.setattr(profiles, "get_access_token", lambda: token)


def test_oauth_only_set_covers_the_refused_actions() -> None:
    assert "api_keys.create" in profiles._OAUTH_ONLY_MANIFEST_TOOL_NAMES
    assert "gateway.provider_credentials.replace" in profiles._OAUTH_ONLY_MANIFEST_TOOL_NAMES
    assert "agents.list" not in profiles._OAUTH_ONLY_MANIFEST_TOOL_NAMES


async def test_api_key_session_does_not_list_oauth_only_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CUSTOMER_PROFILE)
    _authenticate_as(monkeypatch, "api_key")
    names = {tool.name for tool in await create_server().list_tools()}
    assert "api_keys.create" not in names
    assert names, "the rest of the surface stays listed"


async def test_oauth_session_still_lists_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CUSTOMER_PROFILE)
    _authenticate_as(monkeypatch, None)
    names = {tool.name for tool in await create_server().list_tools()}
    assert "api_keys.create" in names


async def test_api_key_session_cannot_call_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CUSTOMER_PROFILE)
    _authenticate_as(monkeypatch, "api_key")
    with pytest.raises(NotFoundError):
        await create_server().call_tool("api_keys.create", {"name": "x"})
