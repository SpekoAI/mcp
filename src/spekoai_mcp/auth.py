"""Authentication configuration for HTTP transport.

The MCP server delegates interactive auth to an upstream OAuth issuer via
FastMCP's `OAuthProxy`. The same protected endpoint also accepts Speko API
keys as bearer tokens for clients that support custom request headers.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
from fastmcp.server.auth import (
    AccessToken,
    JWTVerifier,
    MultiAuth,
    OAuthProxy,
    TokenVerifier,
)
from fastmcp.server.auth.jwt_issuer import derive_jwt_key
from fastmcp.utilities.logging import get_logger

DEFAULT_MCP_PATH = "/mcp"

# Scopes the proxy advertises to downstream MCP clients via its
# `/.well-known/oauth-authorization-server` metadata (`scopes_supported`).
# `offline_access` is load-bearing: MCP clients (Claude Code et al.) only
# request `offline_access` when the server advertises it (the SDK's scope
# selection is WWW-Authenticate scope → PRM `scopes_supported` → client
# metadata scope, all empty today), and Better Auth's oauth-provider only
# mints a refresh token when the granted scope contains `offline_access`
# (@better-auth/oauth-provider dist/index.mjs: `scopes.includes("offline_access")`).
# Without advertising it, the client receives only a ~1h JWT access token and
# no refresh token, so it must redo the full browser auth on every restart.
# These four match oauth-provider's default supported scopes. `valid_scopes`
# is advertise-only — it does not enforce scopes on inbound tokens (that's the
# verifier's `required_scopes`, which we deliberately leave unset).
OAUTH_ADVERTISED_SCOPES = ["openid", "profile", "email", "offline_access"]

# Collection-name prefix for the shared OAuth state in Redis, so proxy keys
# are namespaced away from other workloads (BullMQ) on a shared instance.
OAUTH_STORAGE_PREFIX = "spekoai-mcp-oauth"

_REDIS_URL_SCHEMES = ("redis://", "rediss://", "unix://")

logger = get_logger(__name__)


class _ScopeNormalizingOAuthProxy(OAuthProxy):
    """`OAuthProxy` that guarantees every client may use the advertised scopes.

    Clients vary in what scope they register with: some DCR clients omit it
    (handled by `default_scopes`), but others send `""` or a partial set,
    clients that registered BEFORE we advertised `offline_access` have an
    empty stored scope, and CIMD clients (Claude Code now sends
    `client_id=https://claude.ai/oauth/claude-code-client-metadata`, whose
    metadata document has no scope field) get a scope derived from
    `required_scopes` — empty for us — on fastmcp 3.2.3 (fixed upstream in
    3.2.4, PrefectHQ/fastmcp#3836). The MCP SDK validates `/authorize` scopes
    against the client's REGISTERED scope, so any client that didn't register
    `openid` fails with `invalid_scope: Client was not registered with scope
    openid`.

    Since we only ever advertise `OAUTH_ADVERTISED_SCOPES`, broadening every
    loaded client to exactly that set is safe and makes the advertised scopes
    always grantable — for new, partial, grandfathered, and CIMD clients
    alike, with no cache-clearing. The scope the client actually REQUESTS is
    still what gets forwarded upstream, so this only relaxes the local subset
    check.
    """

    async def get_client(self, client_id: str):  # type: ignore[override]
        client = await super().get_client(client_id)
        if client is None:
            return None
        # Return a COPY with the scope normalized rather than mutating the stored
        # client object in place — the underlying store may hand back a shared/
        # cached instance, and aliasing the change back into it is surprising.
        return client.model_copy(update={"scope": " ".join(OAUTH_ADVERTISED_SCOPES)})


class SpekoApiKeyVerifier(TokenVerifier):
    """Validate Speko API keys against the Speko API."""

    def __init__(self, *, api_base_url: str | None = None) -> None:
        super().__init__(required_scopes=["api_key"])
        self.api_base_url = (
            api_base_url or os.environ.get("SPEKOAI_API_URL") or "https://api.speko.dev"
        ).rstrip("/")

    async def verify_token(self, token: str) -> AccessToken | None:
        # Speko API keys are opaque `sk_*` tokens. Let the OAuth verifier handle
        # JWT-shaped tokens and avoid probing the API for unrelated bearer values.
        if not token.startswith("sk_"):
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.api_base_url}/v1/organization",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            logger.debug("Speko API key verification request failed", exc_info=True)
            return None

        if resp.status_code in {401, 403}:
            return None
        if resp.status_code != 200:
            logger.debug(
                "Speko API key verification returned unexpected status %s",
                resp.status_code,
            )
            return None

        claims: dict[str, Any] = {"auth_method": "api_key"}
        try:
            payload = resp.json()
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                claims["organization_id"] = payload["id"]
        except ValueError:
            pass

        client_id = (
            f"api-key:{claims['organization_id']}" if "organization_id" in claims else "api-key"
        )
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=["api_key"],
            expires_at=None,
            claims=claims,
        )


_ENV_FLAG_TRUE = {"1", "true", "yes", "on"}
_ENV_FLAG_FALSE = {"", "0", "false", "no", "off"}

# Canonical Fernet key: urlsafe-base64 of exactly 32 bytes (43 chars + '=').
# Strict on purpose — a lenient base64 decode would silently discard invalid
# characters and misclassify passphrases as Fernet keys, skipping the KDF.
_FERNET_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}=")


def _env_flag(name: str) -> bool:
    """Strict boolean env parsing — a typo must not silently mean False."""
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in _ENV_FLAG_TRUE:
        return True
    if raw in _ENV_FLAG_FALSE:
        return False
    raise ValueError(
        f"{name} must be one of {sorted(_ENV_FLAG_TRUE | _ENV_FLAG_FALSE - {''})} "
        f"or unset (got: {raw!r})."
    )


def _load_jwt_signing_key() -> bytes | None:
    """Read the fixed proxy JWT signing key from the environment.

    Returns `None` when unset or empty (FastMCP then derives a key from the
    upstream client secret — deterministic across instances, but coupled to
    secret rotation). A canonical Fernet-format value (urlsafe-base64 of 32
    bytes, from `Fernet.generate_key()`) is used as-is; any other string is
    derived via FastMCP's own PBKDF2 path so the resulting key matches what
    FastMCP would derive from the same string. Rotating this key invalidates
    every outstanding proxy-issued token AND the storage encryption derived
    from it (forced re-auth for all users) — treat it as long-lived.
    """
    raw = (os.environ.get("SPEKOAI_OAUTH_JWT_SIGNING_KEY") or "").strip()
    if not raw:
        # Present-but-empty means unset — matches how every other env var in
        # this module is read (Cloud Run renders removed vars as "").
        return None
    if len(raw) < 32:
        raise ValueError(
            "SPEKOAI_OAUTH_JWT_SIGNING_KEY must be at least 32 characters; "
            "generate one with: python -c 'from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())'"
        )
    if _FERNET_KEY_RE.fullmatch(raw):
        # FastMCP uses bytes verbatim as the HS256 key (and `.decode()`s them
        # to derive its default storage-encryption key).
        return raw.encode()
    # Same derivation FastMCP applies to a str key (PBKDF2, 1M iterations —
    # one-time at startup). Deriving here, once, lets the storage encryption
    # key below reuse the result instead of paying the KDF twice.
    return derive_jwt_key(low_entropy_material=raw, salt="fastmcp-jwt-signing-key")


def _create_redis_client(url: str):
    """Build the async Redis client for the shared OAuth state store.

    `redis.asyncio.Redis.from_url` (unlike RedisStore's own URL parsing)
    honors `rediss://` TLS, auth, and DB-index URL components.
    `decode_responses=True` is required by RedisStore's serialization.
    Split out as a seam so tests can substitute fakeredis.
    """
    from redis.asyncio import Redis

    return Redis.from_url(url, decode_responses=True)


def _build_client_storage(redis_url: str, jwt_signing_key: bytes):
    """Shared, encrypted, namespaced `client_storage` for the OAuthProxy.

    Mirrors FastMCP's own default storage construction (Fernet encryption
    with a key HKDF-derived from the JWT signing key, decryption errors
    treated as cache misses) but backed by Redis instead of per-instance
    disk, so DCR registrations, transactions, auth codes, JTI mappings, and
    refresh-token state survive restarts and are shared across instances.
    """
    from cryptography.fernet import Fernet
    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper

    store = RedisStore(client=_create_redis_client(redis_url))
    namespaced = PrefixCollectionsWrapper(key_value=store, prefix=OAUTH_STORAGE_PREFIX)
    storage_key = derive_jwt_key(
        high_entropy_material=jwt_signing_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )
    return FernetEncryptionWrapper(
        key_value=namespaced,
        fernet=Fernet(key=storage_key),
        raise_on_decryption_error=False,
    )


def _shared_state_config() -> tuple[bytes | None, str | None, bool]:
    """Read + cross-validate the opt-in shared-state env vars.

    The three vars form a ladder (each step requires the ones before it):

    1. SPEKOAI_OAUTH_JWT_SIGNING_KEY — fixed key for proxy-minted JWTs, so
       tokens survive restarts/deploys independent of client-secret rotation.
    2. SPEKOAI_OAUTH_REDIS_URL — shared OAuth state storage. Requires (1):
       the storage encryption key is derived from the signing key.
    3. SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS — advertise the OIDC scopes +
       `offline_access` so clients obtain refresh tokens. Requires (1)+(2):
       advertising scopes without shared state is exactly the 0.1.9–0.1.11
       config that broke sign-in on multi-instance Cloud Run
       ("Authorization session mismatch", reverted in 0.1.12 / #757), so it
       fails closed here.

    All unset → legacy 0.1.12/0.1.13 behavior, byte-identical.
    """
    jwt_signing_key = _load_jwt_signing_key()
    redis_url = (os.environ.get("SPEKOAI_OAUTH_REDIS_URL") or "").strip() or None
    advertise = _env_flag("SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS")

    if redis_url and not redis_url.startswith(_REDIS_URL_SCHEMES):
        # Never echo the URL itself — managed Redis URLs embed credentials in
        # the userinfo, and this ValueError lands in crash-loop logs.
        scheme = redis_url.split("://", 1)[0] + "://" if "://" in redis_url else "<no scheme>"
        raise ValueError(
            "SPEKOAI_OAUTH_REDIS_URL must start with one of "
            f"{', '.join(_REDIS_URL_SCHEMES)} (got scheme: {scheme}; value "
            "withheld — it may embed credentials)."
        )
    if redis_url and jwt_signing_key is None:
        raise ValueError(
            "SPEKOAI_OAUTH_REDIS_URL requires SPEKOAI_OAUTH_JWT_SIGNING_KEY: "
            "the shared store's encryption key is derived from the signing "
            "key, and a fixed key keeps proxy-issued tokens valid across "
            "restarts independent of upstream client-secret rotation."
        )
    if advertise and not (redis_url and jwt_signing_key):
        raise ValueError(
            "SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS requires both "
            "SPEKOAI_OAUTH_JWT_SIGNING_KEY and SPEKOAI_OAUTH_REDIS_URL. "
            "Advertising scopes without shared consent/transaction state is "
            "the 0.1.9 configuration that failed with 'Authorization session "
            "mismatch' on multi-instance Cloud Run (reverted in 0.1.12, #757)."
        )
    return jwt_signing_key, redis_url, advertise


def build_auth(mcp_path: str = DEFAULT_MCP_PATH) -> MultiAuth:
    """Return auth for the hosted MCP endpoint.

    If the OAuth issuer/client env vars are absent, the endpoint still requires
    a Speko API key (`Authorization: Bearer sk_*`). Partial OAuth configuration
    raises `ValueError` so deployment fails closed. When OAuth is configured,
    API-key verification remains enabled so clients may authenticate with
    either OAuth or a Speko API key.

    Silent token refresh (opt-in, see `_shared_state_config`): setting
    SPEKOAI_OAUTH_JWT_SIGNING_KEY + SPEKOAI_OAUTH_REDIS_URL +
    SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS gives the proxy a fixed signing
    key, shared Redis-backed OAuth state, and advertised `offline_access`,
    so MCP clients receive refresh tokens and stop re-authing via the
    browser every ~1h / restart. All three unset → legacy behavior.
    """
    oauth_env = {
        "SPEKOAI_OAUTH_ISSUER": os.environ.get("SPEKOAI_OAUTH_ISSUER"),
        "SPEKOAI_OAUTH_CLIENT_ID": os.environ.get("SPEKOAI_OAUTH_CLIENT_ID"),
        "SPEKOAI_OAUTH_CLIENT_SECRET": os.environ.get("SPEKOAI_OAUTH_CLIENT_SECRET"),
    }
    configured_oauth = {name for name, value in oauth_env.items() if value}
    base_url = os.environ.get("SPEKOAI_MCP_BASE_URL")
    # Validate the shared-state ladder even in API-key-only mode — a
    # half-migrated env (shared-state vars present, OAuth base config lost)
    # must fail loudly instead of silently degrading to API-key-only.
    jwt_signing_key, redis_url, advertise_scopes = _shared_state_config()
    if not configured_oauth:
        if jwt_signing_key is not None or redis_url or advertise_scopes:
            raise ValueError(
                "SPEKOAI_OAUTH_JWT_SIGNING_KEY / SPEKOAI_OAUTH_REDIS_URL / "
                "SPEKOAI_OAUTH_ADVERTISE_OFFLINE_ACCESS configure the OAuth "
                "proxy and do nothing in API-key-only mode. Set the "
                "SPEKOAI_OAUTH_* issuer/client vars too, or unset them."
            )
        return MultiAuth(
            verifiers=[SpekoApiKeyVerifier()],
            base_url=base_url or "https://mcp.speko.ai",
        )

    env = {**oauth_env, "SPEKOAI_MCP_BASE_URL": base_url}
    missing = sorted(name for name, value in env.items() if not value)
    if missing:
        raise ValueError(
            "OAuth configuration is incomplete; missing "
            f"{', '.join(missing)}. Either set all required SPEKOAI_OAUTH_* "
            "vars plus SPEKOAI_MCP_BASE_URL, or unset OAuth vars to run "
            "API-key-only."
        )

    issuer = env["SPEKOAI_OAUTH_ISSUER"]
    client_id = env["SPEKOAI_OAUTH_CLIENT_ID"]
    client_secret = env["SPEKOAI_OAUTH_CLIENT_SECRET"]
    base_url = env["SPEKOAI_MCP_BASE_URL"]
    assert issuer and client_id and client_secret and base_url

    # Better Auth mounts OIDC discovery one segment above the oauth-provider
    # endpoints: SPEKOAI_OAUTH_ISSUER ends at `/oauth2`, but the `iss` claim
    # on emitted tokens and the JWKS URL live at the parent path. Validate
    # the suffix explicitly — `rsplit` would silently no-op on a mismatched
    # issuer and leave us verifying tokens against the wrong URL.
    oauth2_suffix = "/oauth2"
    if not issuer.endswith(oauth2_suffix):
        raise ValueError(
            f"SPEKOAI_OAUTH_ISSUER must end with {oauth2_suffix!r} "
            f"(got: {issuer!r}). Point it at the Better Auth oauth-provider "
            "mount, e.g. https://platform.example.com/api/auth/oauth2."
        )
    token_issuer = issuer[: -len(oauth2_suffix)]

    # RFC 8707 resource indicator for this MCP deployment. Better Auth's
    # oauth-provider only mints a JWT (rather than an opaque random
    # string) when the authorize/token request carries a `resource`
    # parameter that matches its `validAudiences` allowlist. We inject
    # one below via `extra_authorize_params` / `extra_token_params`, and
    # the JWT's `aud` claim then matches this value.
    #
    # Default matches FastMCP's own resource URL (`{base_url}{mcp_path}`),
    # which FastMCP normalizes via `base_url.rstrip("/") + "/" + path`
    # (see `fastmcp/server/auth/auth.py::_get_resource_url`). Strip a
    # trailing slash on `base_url` here too — otherwise a
    # `SPEKOAI_MCP_BASE_URL=https://host/` produces `//mcp` and the
    # `aud` claim stops matching FastMCP's advertised resource URL.
    # Override via SPEKOAI_OAUTH_AUDIENCE if the protected MCP endpoint
    # needs a non-default resource indicator.
    normalized_path = "/" + mcp_path.strip("/")
    audience = os.environ.get(
        "SPEKOAI_OAUTH_AUDIENCE",
        f"{base_url.rstrip('/')}{normalized_path}",
    )

    # Opt-in shared-state / refresh-token support (SPE-142), validated by
    # `_shared_state_config()` above. All three env vars unset →
    # `extra_kwargs` stays empty and `proxy_cls` is the plain `OAuthProxy`,
    # i.e. exactly the 0.1.12/0.1.13 construction below.
    extra_kwargs: dict[str, Any] = {}
    if jwt_signing_key is not None:
        extra_kwargs["jwt_signing_key"] = jwt_signing_key
    if redis_url and jwt_signing_key is not None:
        extra_kwargs["client_storage"] = _build_client_storage(redis_url, jwt_signing_key)
    if advertise_scopes:
        # Advertise `offline_access` (+ the standard OIDC scopes) so clients
        # request it and Better Auth mints a refresh token — otherwise clients
        # get a ~1h JWT with no refresh token and re-auth via the browser on
        # every restart. See `OAUTH_ADVERTISED_SCOPES`.
        extra_kwargs["valid_scopes"] = OAUTH_ADVERTISED_SCOPES

    proxy_cls = _ScopeNormalizingOAuthProxy if advertise_scopes else OAuthProxy
    oauth = proxy_cls(
        upstream_authorization_endpoint=f"{issuer}/authorize",
        upstream_token_endpoint=f"{issuer}/token",
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=JWTVerifier(
            jwks_uri=f"{token_issuer}/jwks",
            issuer=token_issuer,
            audience=audience,
        ),
        base_url=base_url,
        # Force the upstream to mint a JWT access token by sending
        # `resource` on both legs of the auth-code flow. Without this
        # the Inspector (and other clients that don't send `resource`
        # themselves) causes Better Auth to emit an opaque token that
        # JWTVerifier can't verify, which the upstream bounces as
        # `invalid_token` on every MCP request — a silent redirect loop.
        # `extra_token_params` also rides refresh-token grants, keeping
        # refreshed access tokens JWTs rather than opaque strings.
        extra_authorize_params={"resource": audience},
        extra_token_params={"resource": audience},
        **extra_kwargs,
    )
    if advertise_scopes:
        # `valid_scopes` only ADVERTISES + bounds scopes; it does not assign
        # any at registration. DCR clients register WITHOUT a scope and only
        # request scopes at `/authorize`, where the MCP SDK validates them
        # against the client's REGISTERED scope. With no `default_scopes`, a
        # no-scope registration leaves the client with an empty scope, so the
        # now-advertised `openid` request fails: `invalid_scope: Client was
        # not registered with scope openid`. Assign `default_scopes` so a
        # no-scope registration is granted exactly what we advertise (and
        # forward upstream). OAuthProxy has no constructor arg for this, so
        # set it on the options before get_routes() runs.
        oauth.client_registration_options.default_scopes = OAUTH_ADVERTISED_SCOPES
    return MultiAuth(server=oauth, verifiers=[SpekoApiKeyVerifier()])
