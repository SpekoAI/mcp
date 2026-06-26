"""Authentication configuration for HTTP transport.

The MCP server delegates interactive auth to an upstream OAuth issuer via
FastMCP's `OAuthProxy`. The same protected endpoint also accepts Speko API
keys as bearer tokens for clients that support custom request headers.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp.server.auth import (
    AccessToken,
    JWTVerifier,
    MultiAuth,
    OAuthProxy,
    TokenVerifier,
)
from fastmcp.utilities.logging import get_logger

DEFAULT_MCP_PATH = "/mcp"

# Scopes the proxy advertises to downstream MCP clients via its
# `/.well-known/oauth-authorization-server` metadata (`scopes_supported`).
# `offline_access` is load-bearing: per MCP SEP-2207 a client (e.g. Claude
# Code) only requests `offline_access` when the authorization server
# advertises it, and Better Auth's oauth-provider only mints a refresh token
# when the granted scope contains `offline_access`
# (@better-auth/oauth-provider dist/index.mjs: `scopes.includes("offline_access")`).
# Without advertising it, the client receives only a ~1h JWT access token and
# no refresh token, so it must redo the full browser auth on every restart.
# These four match oauth-provider's default supported scopes. `valid_scopes`
# is advertise-only — it does not enforce scopes on inbound tokens (that's the
# verifier's `required_scopes`, which we deliberately leave unset).
OAUTH_ADVERTISED_SCOPES = ["openid", "profile", "email", "offline_access"]

logger = get_logger(__name__)


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


def build_auth(mcp_path: str = DEFAULT_MCP_PATH) -> MultiAuth:
    """Return auth for the hosted MCP endpoint.

    If the OAuth issuer/client env vars are absent, the endpoint still requires
    a Speko API key (`Authorization: Bearer sk_*`). Partial OAuth configuration
    raises `ValueError` so deployment fails closed. When OAuth is configured,
    API-key verification remains enabled so clients may authenticate with
    either OAuth or a Speko API key.
    """
    oauth_env = {
        "SPEKOAI_OAUTH_ISSUER": os.environ.get("SPEKOAI_OAUTH_ISSUER"),
        "SPEKOAI_OAUTH_CLIENT_ID": os.environ.get("SPEKOAI_OAUTH_CLIENT_ID"),
        "SPEKOAI_OAUTH_CLIENT_SECRET": os.environ.get("SPEKOAI_OAUTH_CLIENT_SECRET"),
    }
    configured_oauth = {name for name, value in oauth_env.items() if value}
    base_url = os.environ.get("SPEKOAI_MCP_BASE_URL")
    if not configured_oauth:
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

    oauth = OAuthProxy(
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
        # Advertise `offline_access` (+ the standard OIDC scopes) so clients
        # request it and Better Auth mints a refresh token — otherwise clients
        # get a ~1h JWT with no refresh token and re-auth via the browser on
        # every restart. See `OAUTH_ADVERTISED_SCOPES`.
        valid_scopes=OAUTH_ADVERTISED_SCOPES,
        # Force the upstream to mint a JWT access token by sending
        # `resource` on both legs of the auth-code flow. Without this
        # the Inspector (and other clients that don't send `resource`
        # themselves) causes Better Auth to emit an opaque token that
        # JWTVerifier can't verify, which the upstream bounces as
        # `invalid_token` on every MCP request — a silent redirect loop.
        extra_authorize_params={"resource": audience},
        extra_token_params={"resource": audience},
    )
    return MultiAuth(server=oauth, verifiers=[SpekoApiKeyVerifier()])
