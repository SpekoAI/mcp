"""Stateless OAuth and API-key authentication for the hosted MCP endpoint.

Better Auth is the OAuth authorization server. This service is only a resource
server: it publishes protected-resource metadata and validates audience-bound
JWTs on every request. It stores no OAuth clients, authorization transactions,
codes, refresh tokens, or protocol sessions.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp.server.auth import (
    AccessToken,
    AuthProvider,
    MultiAuth,
    RemoteAuthProvider,
    TokenVerifier,
)
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.utilities.logging import get_logger

from spekoai_mcp.profiles import current_profile, profile_serves_phone_calls

DEFAULT_MCP_PATH = "/mcp"
DEFAULT_API_BASE_URL = "https://api.speko.dev"

# These are initial resource scopes, not the complete authorization-server
# scope catalog. In particular, offline_access belongs in Better Auth's AS
# metadata but MUST NOT be advertised as a protected-resource requirement.
OAUTH_MCP_PHONE_SCOPE = "speko:phone"

# Every scope this server can ask for. What a given deployment actually
# advertises is `oauth_resource_scopes()`, which is profile-aware.
OAUTH_RESOURCE_SCOPES = [
    "openid",
    "profile",
    "email",
    "speko:read",
    "speko:write",
    "speko:execute",
    "speko:billing",
    "speko:credentials",
    "speko:compliance",
    # Advertised so the CLIENT requests it -- but only on a surface that can
    # actually dial; see `oauth_resource_scopes()`. It was previously stamped
    # onto the grant by the authorization server instead, which cannot work:
    # when the user has no session -- the normal case for a fresh connector --
    # Better Auth redirects to the login page using `ctx.request.url`, the raw
    # inbound URL, so a scope injected into `ctx.query` by a before-hook is
    # discarded microseconds later and never reaches the consent screen or the
    # token.
    #
    # Requesting it does not grant phone access. Dialing is behind two
    # independent gates (see apps/server/src/routes/sessions-phone.ts): this
    # scope, and the workspace phone-consent row that records the attestation,
    # terms version and disclosed prices. This list only decides what the user
    # is asked to approve.
    OAUTH_MCP_PHONE_SCOPE,
]


def oauth_resource_scopes(profile: str | None = None) -> list[str]:
    """The scopes THIS deployment advertises.

    `speko:phone` is withheld from a surface that serves no calling tool. The
    `builder` preset is one: advertising there would ask a v0/Lovable/Bolt user
    to approve phone access and then send them through the attestation and
    pricing clickwrap -- `postLogin.shouldRedirect` keys that screen off this
    scope -- for tools that profile does not expose.
    """
    resolved = current_profile() if profile is None else profile
    if profile_serves_phone_calls(resolved):
        return list(OAUTH_RESOURCE_SCOPES)
    return [scope for scope in OAUTH_RESOURCE_SCOPES if scope != OAUTH_MCP_PHONE_SCOPE]

logger = get_logger(__name__)


def resource_url(base_url: str, mcp_path: str = DEFAULT_MCP_PATH) -> str:
    """Return the canonical RFC 8707 resource identifier for this MCP endpoint."""
    return f"{base_url.rstrip('/')}/{mcp_path.lstrip('/')}"


class SpekoApiKeyVerifier(TokenVerifier):
    """Verify an opaque Platform API key on every MCP request.

    The verifier deliberately has no cache or durable state. Platform is the
    authority for revocation, organization membership, and scopes, so each
    request observes the current key context.
    """

    def __init__(self, *, api_base_url: str | None = None) -> None:
        super().__init__()
        base = api_base_url or os.environ.get("SPEKOAI_API_URL") or DEFAULT_API_BASE_URL
        self.context_url = f"{base.rstrip('/')}/v1/auth/api-key-context"

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith("sk_"):
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                response = await client.get(
                    self.context_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError:
            logger.debug("Platform API-key context request failed", exc_info=True)
            return None

        if response.status_code != 200:
            return None

        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None

        api_key_id = payload.get("apiKeyId")
        organization_id = payload.get("organizationId")
        raw_scopes = payload.get("scopes")
        if (
            not isinstance(api_key_id, str)
            or not api_key_id
            or not isinstance(organization_id, str)
            or not organization_id
            or not isinstance(raw_scopes, list)
            or any(not isinstance(scope, str) for scope in raw_scopes)
        ):
            return None

        scopes = list(dict.fromkeys(raw_scopes))
        claims: dict[str, Any] = {
            "auth_method": "api_key",
            "api_key_id": api_key_id,
            "organization_id": organization_id,
            "scopes": scopes,
        }
        return AccessToken(
            token=token,
            client_id=f"api-key:{api_key_id}",
            scopes=scopes,
            expires_at=None,
            claims=claims,
        )


def _oauth_auth_provider(*, issuer: str, base_url: str, mcp_path: str) -> RemoteAuthProvider:
    protected_resource = resource_url(base_url, mcp_path)
    verifier = JWTVerifier(
        jwks_uri=f"{issuer}/jwks",
        issuer=issuer,
        audience=protected_resource,
    )
    advertised_scopes = oauth_resource_scopes()
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[issuer],
        base_url=base_url,
        scopes_supported=advertised_scopes,
        challenge_scopes=advertised_scopes,
        resource_name="Speko MCP",
    )


def build_auth(*, mcp_path: str = DEFAULT_MCP_PATH) -> AuthProvider:
    """Build stateless auth for OAuth clients plus direct Platform API keys.

    OAuth is enabled when ``SPEKOAI_OAUTH_ISSUER`` is set. The value is the
    authorization-server issuer (for Better Auth, ``.../api/auth``), not its
    ``.../oauth2`` operational endpoint prefix. A stable delegation secret is
    required because OAuth-authenticated tool calls mint a separate, short-lived
    Platform API token rather than passing the MCP access token upstream.
    """
    issuer = (os.environ.get("SPEKOAI_OAUTH_ISSUER") or "").strip().rstrip("/")
    if not issuer:
        return SpekoApiKeyVerifier()

    base_url = (os.environ.get("SPEKOAI_MCP_BASE_URL") or "").strip().rstrip("/")
    delegation_secret = (os.environ.get("SPEKOAI_MCP_DELEGATION_SECRET") or "").strip()
    missing = [
        name
        for name, value in (
            ("SPEKOAI_MCP_BASE_URL", base_url),
            ("SPEKOAI_MCP_DELEGATION_SECRET", delegation_secret),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "OAuth configuration is incomplete; missing "
            f"{', '.join(missing)}. Set them or unset SPEKOAI_OAUTH_ISSUER "
            "to run API-key-only."
        )
    if issuer.endswith("/oauth2"):
        raise ValueError(
            "SPEKOAI_OAUTH_ISSUER must be the authorization-server issuer "
            "(for Better Auth, the URL ending in /api/auth), not the /oauth2 "
            "operational endpoint prefix."
        )
    if len(delegation_secret) < 32:
        raise ValueError("SPEKOAI_MCP_DELEGATION_SECRET must be at least 32 characters")

    oauth = _oauth_auth_provider(issuer=issuer, base_url=base_url, mcp_path=mcp_path)
    return MultiAuth(
        server=oauth,
        verifiers=[SpekoApiKeyVerifier()],
        base_url=base_url,
    )
