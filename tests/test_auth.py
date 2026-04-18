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
    # The override must also thread through to the upstream `resource`
    # param — otherwise Better Auth mints a JWT with a different `aud`
    # than JWTVerifier expects. All three values share one source.
    assert proxy._extra_authorize_params == {"resource": "https://mcp.example.com"}
    assert proxy._extra_token_params == {"resource": "https://mcp.example.com"}


def test_trailing_slash_on_base_url_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://mcp.example.com/")

    proxy = build_auth()
    assert proxy is not None
    # FastMCP internally normalizes `base_url.rstrip("/") + "/mcp"`
    # when computing its advertised resource URL; if we don't do the
    # same the `aud`/`resource` comparison fails with `//mcp`.
    assert proxy._token_validator.audience == "https://mcp.example.com/mcp"
    assert proxy._extra_authorize_params == {"resource": "https://mcp.example.com/mcp"}


def test_audience_defaults_to_resource_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)

    proxy = build_auth()
    assert proxy is not None
    # Default audience is the MCP resource URL (`{base_url}/mcp`). This
    # must match the `resource` param FastMCP forwards upstream — see
    # `extra_authorize_params` below — so Better Auth's oauth-provider
    # mints a JWT with the same `aud` claim.
    assert proxy._token_validator.audience == "https://mcp.example.com/mcp"


def test_resource_is_forwarded_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)

    proxy = build_auth()
    assert proxy is not None
    # Without `resource` on the upstream authorize/token request, Better
    # Auth falls back to opaque tokens that JWTVerifier can't validate.
    assert proxy._extra_authorize_params == {"resource": "https://mcp.example.com/mcp"}
    assert proxy._extra_token_params == {"resource": "https://mcp.example.com/mcp"}
