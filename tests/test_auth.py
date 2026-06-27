"""Tests for `spekoai_mcp.auth.build_auth`.

These exercise the pure-configuration branches without touching the network:
partial-env short-circuit and the `/oauth2` suffix guard. The happy path
constructs a real `MultiAuth` wrapping `OAuthProxy` plus API-key verification.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import spekoai_mcp.auth as auth_module
from spekoai_mcp.auth import SpekoApiKeyVerifier, build_auth

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


def _oauth_proxy(auth):
    assert auth is not None
    return auth.server


def test_returns_api_key_auth_when_oauth_unset() -> None:
    auth = build_auth()
    assert auth.server is None
    assert any(isinstance(v, SpekoApiKeyVerifier) for v in auth.verifiers)


def test_rejects_partial_oauth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    # client_secret + base_url missing -> fail closed, not API-key fallback
    with pytest.raises(ValueError, match="SPEKOAI_MCP_BASE_URL"):
        build_auth()


def test_rejects_oauth_when_base_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # No prod-domain fallback for OAuth: forgetting SPEKOAI_MCP_BASE_URL must not
    # silently redirect OAuth traffic at the prod host.
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="SPEKOAI_MCP_BASE_URL"):
        build_auth()


def test_rejects_issuer_without_oauth2_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth")

    with pytest.raises(ValueError, match="/oauth2"):
        build_auth()


def test_builds_proxy_for_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    auth = build_auth()
    assert auth is not None
    assert any(isinstance(v, SpekoApiKeyVerifier) for v in auth.verifiers)


def test_audience_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_AUDIENCE", "https://mcp.example.com")

    proxy = _oauth_proxy(build_auth())
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

    proxy = _oauth_proxy(build_auth())
    # FastMCP internally normalizes `base_url.rstrip("/") + "/mcp"`
    # when computing its advertised resource URL; if we don't do the
    # same the `aud`/`resource` comparison fails with `//mcp`.
    assert proxy._token_validator.audience == "https://mcp.example.com/mcp"
    assert proxy._extra_authorize_params == {"resource": "https://mcp.example.com/mcp"}


def test_audience_defaults_to_resource_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)

    proxy = _oauth_proxy(build_auth())
    # Default audience is the protected MCP resource URL
    # (`{base_url}/mcp`). This
    # must match the `resource` param FastMCP forwards upstream — see
    # `extra_authorize_params` below — so Better Auth's oauth-provider
    # mints a JWT with the same `aud` claim.
    assert proxy._token_validator.audience == "https://mcp.example.com/mcp"


def test_resource_is_forwarded_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)

    proxy = _oauth_proxy(build_auth())
    # Without `resource` on the upstream authorize/token request, Better
    # Auth falls back to opaque tokens that JWTVerifier can't validate.
    assert proxy._extra_authorize_params == {"resource": "https://mcp.example.com/mcp"}
    assert proxy._extra_token_params == {"resource": "https://mcp.example.com/mcp"}


def test_custom_mcp_path_sets_default_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)

    proxy = _oauth_proxy(build_auth(mcp_path="/internal/private"))
    assert proxy._token_validator.audience == "https://mcp.example.com/internal/private"
    assert proxy._extra_authorize_params == {"resource": "https://mcp.example.com/internal/private"}


async def test_api_key_verifier_accepts_valid_speko_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> object:
            captured["url"] = url
            captured["headers"] = headers
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "org_123"},
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeAsyncClient)

    verifier = SpekoApiKeyVerifier(api_base_url="https://api.example")
    token = await verifier.verify_token("sk_live_test")

    assert captured["url"] == "https://api.example/v1/organization"
    assert captured["headers"] == {"Authorization": "Bearer sk_live_test"}
    assert token is not None
    assert token.token == "sk_live_test"
    assert token.client_id == "api-key:org_123"
    assert token.scopes == ["api_key"]
    assert token.claims == {"auth_method": "api_key", "organization_id": "org_123"}


async def test_api_key_verifier_rejects_non_speko_token() -> None:
    verifier = SpekoApiKeyVerifier(api_base_url="https://api.example")
    assert await verifier.verify_token("eyJ.not.an.api.key") is None


async def test_api_key_verifier_rejects_invalid_speko_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> object:
            return SimpleNamespace(status_code=401)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeAsyncClient)

    verifier = SpekoApiKeyVerifier(api_base_url="https://api.example")
    assert await verifier.verify_token("sk_live_bad") is None
