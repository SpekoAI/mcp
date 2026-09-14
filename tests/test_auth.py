"""Tests for stateless OAuth and Platform API-key authentication."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.server.auth import MultiAuth, RemoteAuthProvider

import spekoai_mcp.auth as auth_module
from spekoai_mcp.auth import (
    OAUTH_RESOURCE_SCOPES,
    SpekoApiKeyVerifier,
    build_auth,
    resource_url,
)

_AUTH_ENV = (
    "SPEKOAI_OAUTH_ISSUER",
    "SPEKOAI_MCP_BASE_URL",
    "SPEKOAI_MCP_DELEGATION_SECRET",
    "SPEKOAI_MCP_DELEGATION_ISSUER",
    "SPEKOAI_API_AUDIENCE",
)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _AUTH_ENV:
        monkeypatch.delenv(name, raising=False)


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://platform.example/api/auth")
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://mcp.example")
    monkeypatch.setenv("SPEKOAI_MCP_DELEGATION_SECRET", "d" * 32)


def test_resource_url_normalizes_slashes() -> None:
    assert resource_url("https://mcp.example/", "/mcp") == "https://mcp.example/mcp"


def test_build_auth_returns_api_key_verifier_when_oauth_is_unset() -> None:
    assert isinstance(build_auth(), SpekoApiKeyVerifier)


def test_oauth_config_fails_closed_when_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://platform.example/api/auth")
    with pytest.raises(ValueError, match="SPEKOAI_MCP_BASE_URL"):
        build_auth()


def test_oauth_rejects_proxy_endpoint_as_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_oauth_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://platform.example/api/auth/oauth2")
    with pytest.raises(ValueError, match="authorization-server issuer"):
        build_auth()


def test_oauth_rejects_short_delegation_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_oauth_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_MCP_DELEGATION_SECRET", "short")
    with pytest.raises(ValueError, match="at least 32"):
        build_auth()


def test_builds_stateless_remote_oauth_plus_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_oauth_env(monkeypatch)
    auth = build_auth()

    assert isinstance(auth, MultiAuth)
    assert isinstance(auth.server, RemoteAuthProvider)
    assert any(isinstance(verifier, SpekoApiKeyVerifier) for verifier in auth.verifiers)
    assert not hasattr(auth.server, "_client_storage")
    assert [str(url) for url in auth.server.authorization_servers] == [
        "https://platform.example/api/auth"
    ]
    assert auth.server.token_verifier.issuer == "https://platform.example/api/auth"
    assert auth.server.token_verifier.jwks_uri == "https://platform.example/api/auth/jwks"
    assert auth.server.token_verifier.audience == "https://mcp.example/mcp"


def test_protected_resource_scopes_exclude_offline_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_oauth_env(monkeypatch)
    auth = build_auth()
    assert isinstance(auth, MultiAuth)
    assert auth.server is not None
    assert auth.server.scopes_supported == OAUTH_RESOURCE_SCOPES
    assert "offline_access" not in auth.server.scopes_supported


def test_custom_mcp_path_binds_oauth_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_oauth_env(monkeypatch)
    auth = build_auth(mcp_path="/internal/mcp")
    assert isinstance(auth, MultiAuth)
    assert isinstance(auth.server, RemoteAuthProvider)
    assert auth.server.token_verifier.audience == "https://mcp.example/internal/mcp"


async def test_verifier_calls_context_endpoint_and_populates_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {"calls": 0}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
            captured["timeout"] = timeout
            captured["follow_redirects"] = follow_redirects

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> object:
            captured["calls"] = int(captured["calls"]) + 1
            captured["url"] = url
            captured["headers"] = headers
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "apiKeyId": "key_123",
                    "organizationId": "org_123",
                    "scopes": ["gateway.keys.manage"],
                },
            )

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeAsyncClient)
    verifier = SpekoApiKeyVerifier(api_base_url="https://api.example/")

    first = await verifier.verify_token("sk_live_test")
    second = await verifier.verify_token("sk_live_test")

    assert captured == {
        "calls": 2,
        "timeout": 10.0,
        "follow_redirects": False,
        "url": "https://api.example/v1/auth/api-key-context",
        "headers": {
            "Authorization": "Bearer sk_live_test",
            "Accept": "application/json",
        },
    }
    assert first is not None and second is not None
    assert first.client_id == "api-key:key_123"
    assert first.scopes == ["gateway.keys.manage"]
    assert first.claims == {
        "auth_method": "api_key",
        "api_key_id": "key_123",
        "organization_id": "org_123",
        "scopes": ["gateway.keys.manage"],
    }


async def test_verifier_rejects_non_api_key_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth_module.httpx,
        "AsyncClient",
        lambda **_: pytest.fail("non-sk token must not reach Platform"),
    )
    assert await SpekoApiKeyVerifier().verify_token("not-an-api-key") is None


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_verifier_rejects_failed_context_response(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    class FakeAsyncClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(status_code=status)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeAsyncClient)
    assert await SpekoApiKeyVerifier().verify_token("sk_bad") is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"apiKeyId": "key_1", "organizationId": "org_1"},
        {"apiKeyId": "key_1", "organizationId": "org_1", "scopes": "bad"},
        {"apiKeyId": "", "organizationId": "org_1", "scopes": []},
    ],
)
async def test_verifier_rejects_invalid_context_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    class FakeAsyncClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(status_code=200, json=lambda: payload)

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeAsyncClient)
    assert await SpekoApiKeyVerifier().verify_token("sk_bad") is None
