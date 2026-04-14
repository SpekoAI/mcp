"""Tests for `spekoai_mcp.auth.build_auth`.

These exercise the pure-configuration branches without touching the network:
partial-env short-circuit and the `/oauth2` suffix guard. The happy path
constructs a real `OAuthProxy`, so we only assert it returns a non-None
object rather than introspect FastMCP internals.
"""

from __future__ import annotations

import pytest

from spekoai_mcp.auth import build_auth

_OAUTH_ENV = (
    "SPEKOAI_OAUTH_ISSUER",
    "SPEKOAI_OAUTH_CLIENT_ID",
    "SPEKOAI_OAUTH_CLIENT_SECRET",
    "SPEKOAI_MCP_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)


def test_returns_none_when_fully_unset() -> None:
    assert build_auth() is None


def test_returns_none_on_partial_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    # client_secret missing -> None, not KeyError
    assert build_auth() is None


def test_rejects_issuer_without_oauth2_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")

    with pytest.raises(ValueError, match="/oauth2"):
        build_auth()


def test_builds_proxy_for_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://mcp.example.com")

    assert build_auth() is not None
