"""Tests for stateless MCP-to-Platform OAuth delegation."""

from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest

from spekoai_mcp.delegation import (
    DELEGATION_ALGORITHM,
    DELEGATION_TOKEN_TYPE,
    DelegationError,
    platform_bearer_token,
)


def test_api_key_is_forwarded_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEKOAI_MCP_DELEGATION_SECRET", raising=False)
    assert platform_bearer_token(SimpleNamespace(token="sk_live_test")) == "sk_live_test"


def test_oauth_principal_receives_separate_api_audience_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "delegation-secret-at-least-32-characters"
    monkeypatch.setenv("SPEKOAI_MCP_DELEGATION_SECRET", secret)
    monkeypatch.setenv("SPEKOAI_MCP_DELEGATION_ISSUER", "https://mcp.example/")
    monkeypatch.setenv("SPEKOAI_API_AUDIENCE", "https://api.example/")

    original = "eyJ.client-facing-mcp-token.signature"
    delegated = platform_bearer_token(
        SimpleNamespace(
            token=original,
            subject="user-1",
            client_id="https://client.example/oauth.json",
            scopes=["openid", "profile"],
            claims={"sub": "user-1"},
        )
    )

    assert delegated != original
    assert jwt.get_unverified_header(delegated)["typ"] == DELEGATION_TOKEN_TYPE
    payload = jwt.decode(
        delegated,
        secret,
        algorithms=[DELEGATION_ALGORITHM],
        issuer="https://mcp.example",
        audience="https://api.example",
    )
    assert payload["sub"] == "user-1"
    assert payload["scope"] == "openid profile"
    assert payload["auth_method"] == "mcp_oauth_delegation"
    assert payload["client_id"] == "https://client.example/oauth.json"
    assert payload["exp"] - payload["iat"] == 60


def test_delegation_requires_validated_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_MCP_DELEGATION_SECRET", "d" * 32)
    with pytest.raises(DelegationError, match="subject"):
        platform_bearer_token(SimpleNamespace(token="oauth-token", claims={}))


def test_delegation_requires_strong_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_MCP_DELEGATION_SECRET", "short")
    with pytest.raises(DelegationError, match="at least 32"):
        platform_bearer_token(
            SimpleNamespace(token="oauth-token", subject="user-1", claims={})
        )
