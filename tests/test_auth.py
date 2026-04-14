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
    "SPEKOAI_OAUTH_AUDIENCE",
    "SPEKOAI_MCP_BASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)


def _set_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://mcp.example.com")


def test_returns_none_when_fully_unset() -> None:
    assert build_auth() is None


def test_returns_none_on_partial_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    # client_secret + base_url missing -> None, not KeyError
    assert build_auth() is None


def test_returns_none_when_base_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # No prod-domain fallback: forgetting SPEKOAI_MCP_BASE_URL must not
    # silently redirect OAuth traffic at the prod host.
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")
    assert build_auth() is None


def test_rejects_issuer_without_oauth2_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth")

    with pytest.raises(ValueError, match="/oauth2"):
        build_auth()


def test_builds_proxy_for_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    assert build_auth() is not None


def test_audience_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_AUDIENCE", "https://mcp.example.com")

    proxy = build_auth()
    assert proxy is not None
    # Audience is stored on the embedded JWTVerifier (private attr in
    # current FastMCP; acceptable — alternative is a real token-verify
    # round-trip that needs a test JWKS fixture).
    assert proxy._token_validator.audience == "https://mcp.example.com"


def test_audience_defaults_to_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)

    proxy = build_auth()
    assert proxy is not None
    assert proxy._token_validator.audience == "id"
