"""Stateless delegation from an MCP OAuth principal to the Platform API."""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

import jwt

DEFAULT_API_AUDIENCE = "https://api.speko.dev"
DEFAULT_MCP_ISSUER = "https://mcp.speko.ai"
DELEGATION_ALGORITHM = "HS256"
DELEGATION_TOKEN_TYPE = "speko-mcp-delegation+jwt"
DELEGATION_TTL_SECONDS = 60


class DelegationError(RuntimeError):
    """Raised when a validated MCP OAuth principal cannot be delegated."""


def _required_secret() -> str:
    secret = (os.environ.get("SPEKOAI_MCP_DELEGATION_SECRET") or "").strip()
    if len(secret) < 32:
        raise DelegationError(
            "OAuth delegation is unavailable: SPEKOAI_MCP_DELEGATION_SECRET "
            "must be configured with at least 32 characters."
        )
    return secret


def platform_bearer_token(access_token: Any) -> str:
    """Return an API key unchanged or mint a short-lived API-audience JWT.

    FastMCP has already validated OAuth signature, issuer, expiry, and the MCP
    resource audience before this function runs. Only the validated subject is
    delegated; the client-presented MCP token is never forwarded upstream.
    """
    token = getattr(access_token, "token", access_token)
    if not isinstance(token, str) or not token:
        raise DelegationError("Authenticated MCP token is missing or invalid.")
    if token.startswith("sk_"):
        return token

    claims = getattr(access_token, "claims", None)
    if not isinstance(claims, dict):
        claims = {}
    subject = getattr(access_token, "subject", None) or claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise DelegationError("Validated OAuth token is missing its subject claim.")

    raw_scopes = getattr(access_token, "scopes", None) or []
    scopes = [scope for scope in raw_scopes if isinstance(scope, str) and scope]
    client_id = getattr(access_token, "client_id", None)
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": (os.environ.get("SPEKOAI_MCP_DELEGATION_ISSUER") or DEFAULT_MCP_ISSUER).rstrip(
            "/"
        ),
        "aud": (
            os.environ.get("SPEKOAI_API_AUDIENCE") or DEFAULT_API_AUDIENCE
        ).rstrip("/"),
        "sub": subject,
        "iat": now,
        "nbf": now - 5,
        "exp": now + DELEGATION_TTL_SECONDS,
        "jti": uuid4().hex,
        "scope": " ".join(scopes),
        "auth_method": "mcp_oauth_delegation",
    }
    if isinstance(client_id, str) and client_id:
        payload["client_id"] = client_id
    organization_id = claims.get("organization_id")
    if isinstance(organization_id, str) and organization_id:
        payload["organization_id"] = organization_id

    return jwt.encode(
        payload,
        _required_secret(),
        algorithm=DELEGATION_ALGORITHM,
        headers={"typ": DELEGATION_TOKEN_TYPE},
    )
