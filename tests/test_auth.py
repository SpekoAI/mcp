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
    "SPEKOAI_OAUTH_JWT_SIGNING_KEY",
    "SPEKOAI_OAUTH_REDIS_URL",
    "SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS",
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


# ---------------------------------------------------------------------------
# Opt-in shared-state / refresh-token config (SPE-142).
#
# Three env vars form a ladder: SPEKOAI_OAUTH_JWT_SIGNING_KEY (fixed proxy
# JWT key) <- SPEKOAI_OAUTH_REDIS_URL (shared OAuth state, requires the key)
# <- SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS (refresh tokens, requires both).
# All unset must be byte-identical to the 0.1.12/0.1.13 construction.
# ---------------------------------------------------------------------------

from cryptography.fernet import Fernet  # noqa: E402
from fastmcp.server.auth import OAuthProxy  # noqa: E402

from spekoai_mcp.auth import (  # noqa: E402
    OAUTH_ADVERTISED_SCOPES,
    OAUTH_STORAGE_PREFIX,
    _ScopeNormalizingOAuthProxy,
)


def _set_shared_state_env(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", key)
    monkeypatch.setenv("SPEKOAI_OAUTH_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS", "true")
    return key


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch):
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(auth_module, "_create_redis_client", lambda url: fake)
    return fake


def test_env_unset_keeps_legacy_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """No shared-state env -> the exact 0.1.12/0.1.13 proxy construction."""
    _set_valid_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())
    assert type(proxy) is OAuthProxy  # not _ScopeNormalizingOAuthProxy
    # valid_scopes falls back to the verifier's (empty) required_scopes;
    # nothing is advertised and no default registration scope is assigned.
    assert not proxy.client_registration_options.valid_scopes
    assert proxy.client_registration_options.default_scopes is None
    # Default FastMCP storage: encrypted file store, NOT our Redis chain
    # (whose first layer under the encryption wrapper is a prefix wrapper).
    from key_value.aio.stores.filetree import FileTreeStore

    assert isinstance(proxy._client_storage.key_value, FileTreeStore)


def test_advertise_requires_shared_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 0.1.9 outage config (scopes without shared state) fails closed."""
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS", "true")
    with pytest.raises(ValueError, match="Authorization session mismatch"):
        build_auth()


def test_redis_requires_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_REDIS_URL", "redis://localhost:6379/1")
    with pytest.raises(ValueError, match="SPEKOAI_OAUTH_JWT_SIGNING_KEY"):
        build_auth()


def test_rejects_non_redis_url_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SPEKOAI_OAUTH_REDIS_URL", "http://localhost:6379")
    with pytest.raises(ValueError, match="SPEKOAI_OAUTH_REDIS_URL"):
        build_auth()


def test_rejects_short_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", "too-short")
    with pytest.raises(ValueError, match="at least 32 characters"):
        build_auth()


def test_fernet_shaped_signing_key_used_verbatim(
    monkeypatch: pytest.MonkeyPatch, fake_redis
) -> None:
    """A Fernet-format key skips the KDF and is passed to FastMCP as bytes."""
    _set_valid_env(monkeypatch)
    key = _set_shared_state_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())
    assert proxy._jwt_signing_key == key.encode()


def test_non_fernet_signing_key_is_derived(monkeypatch: pytest.MonkeyPatch, fake_redis) -> None:
    """A plain-string key is derived exactly as FastMCP would derive it."""
    import fastmcp
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    # 10 KDF iterations instead of 1M; parity with prod is by construction
    # (same call, same salt), not something this test measures.
    monkeypatch.setattr(fastmcp.settings, "test_mode", True)
    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    raw = "not-base64-material-but-long-enough-to-pass-validation"
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", raw)
    proxy = _oauth_proxy(build_auth())
    expected = derive_jwt_key(low_entropy_material=raw, salt="fastmcp-jwt-signing-key")
    assert proxy._jwt_signing_key == expected


def test_advertises_offline_access_scope(monkeypatch: pytest.MonkeyPatch, fake_redis) -> None:
    """valid_scopes drives `scopes_supported` in the discovery metadata."""
    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())
    assert type(proxy) is _ScopeNormalizingOAuthProxy
    valid_scopes = proxy.client_registration_options.valid_scopes
    assert valid_scopes is not None
    assert "offline_access" in valid_scopes
    assert {"openid", "profile", "email"} <= set(valid_scopes)


def test_registers_dcr_clients_with_default_scopes(
    monkeypatch: pytest.MonkeyPatch, fake_redis
) -> None:
    """No-scope DCR registrations are granted the advertised scopes."""
    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())
    default_scopes = proxy.client_registration_options.default_scopes
    assert default_scopes == OAUTH_ADVERTISED_SCOPES


@pytest.mark.parametrize("registered_scope", ["", "offline_access"])
async def test_get_client_normalizes_empty_or_partial_scope(
    monkeypatch: pytest.MonkeyPatch, fake_redis, registered_scope: str
) -> None:
    """Empty/partial registrations may still use every advertised scope.

    `default_scopes` only covers an OMITTED scope; clients that send "" or a
    partial set (and clients grandfathered from before the scopes were
    advertised) fail the SDK's /authorize registered-scope check without
    `get_client` normalization.
    """
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())

    client_id = f"client-{registered_scope or 'empty'}"
    await proxy.register_client(
        OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None,
            redirect_uris=[AnyUrl("http://localhost:1234/cb")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=registered_scope,
        )
    )

    loaded = await proxy.get_client(client_id)
    assert loaded is not None
    assert loaded.scope is not None
    assert "openid" in loaded.scope
    assert "offline_access" in loaded.scope


async def test_get_client_normalizes_grandfathered_client(
    monkeypatch: pytest.MonkeyPatch, fake_redis
) -> None:
    """A client stored before scopes were advertised is normalized on load."""
    from fastmcp.server.auth.oauth_proxy.models import ProxyDCRClient
    from pydantic import AnyUrl

    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())

    # Seed the shared store directly, bypassing register_client — this is
    # what a pre-0.1.9 registration row looks like: empty scope.
    await proxy._client_store.put(
        key="grandfathered",
        value=ProxyDCRClient(
            client_id="grandfathered",
            client_secret=None,
            redirect_uris=[AnyUrl("http://localhost:9999/cb")],
            grant_types=["authorization_code", "refresh_token"],
            scope="",
            token_endpoint_auth_method="none",
        ),
    )

    loaded = await proxy.get_client("grandfathered")
    assert loaded is not None
    assert loaded.scope == " ".join(OAUTH_ADVERTISED_SCOPES)
    # Normalization must not write back into the store (returns a copy).
    stored = await proxy._client_store.get(key="grandfathered")
    assert stored is not None
    assert stored.scope == ""


def test_redis_without_advertise_is_a_valid_staging_config(
    monkeypatch: pytest.MonkeyPatch, fake_redis
) -> None:
    """Key + Redis without scope advertising: shared state, legacy scopes.

    This is rollout step 1 — it already fixes cross-instance 401s (the JTI
    mapping lookup on every request) without changing the sign-in flow.
    """
    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    monkeypatch.delenv("SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS")

    proxy = _oauth_proxy(build_auth())
    assert type(proxy) is OAuthProxy
    assert not proxy.client_registration_options.valid_scopes
    assert proxy.client_registration_options.default_scopes is None

    storage = proxy._client_storage
    assert isinstance(storage, FernetEncryptionWrapper)
    assert isinstance(storage.key_value, PrefixCollectionsWrapper)
    assert storage.key_value.prefix == OAUTH_STORAGE_PREFIX
    assert isinstance(storage.key_value.key_value, RedisStore)


def test_env_flag_rejects_unrecognized_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd flag must fail loudly, not silently deploy legacy behavior."""
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS", "ture")
    with pytest.raises(ValueError, match="SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS"):
        build_auth()


def test_empty_signing_key_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present-but-empty env (how Cloud Run renders removed vars) == unset."""
    _set_valid_env(monkeypatch)
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", "")
    monkeypatch.setenv("SPEKOAI_OAUTH_REDIS_URL", "")
    monkeypatch.setenv("SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS", "")
    proxy = _oauth_proxy(build_auth())
    assert type(proxy) is OAuthProxy


def test_shared_state_vars_without_oauth_fail_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-migrated env (shared-state vars but no OAuth base config) must
    not silently degrade to API-key-only mode."""
    monkeypatch.setenv("SPEKOAI_OAUTH_JWT_SIGNING_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="API-key-only"):
        build_auth()


async def test_normalized_client_still_rejects_unadvertised_scope(
    monkeypatch: pytest.MonkeyPatch, fake_redis
) -> None:
    """Normalization only relaxes the check UP TO the advertised set.

    Pins the security bound: a scope outside OAUTH_ADVERTISED_SCOPES must
    still fail the SDK's registered-scope validation.
    """
    from mcp.shared.auth import InvalidScopeError, OAuthClientInformationFull
    from pydantic import AnyUrl

    _set_valid_env(monkeypatch)
    _set_shared_state_env(monkeypatch)
    proxy = _oauth_proxy(build_auth())
    await proxy.register_client(
        OAuthClientInformationFull(
            client_id="bounded",
            client_secret=None,
            redirect_uris=[AnyUrl("http://localhost:1234/cb")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope="",
        )
    )
    loaded = await proxy.get_client("bounded")
    assert loaded is not None
    assert loaded.validate_scope("openid offline_access") is not None
    with pytest.raises(InvalidScopeError):
        loaded.validate_scope("openid admin")
