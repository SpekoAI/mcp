"""OAuth configuration for HTTP transport.

The MCP server delegates auth to an upstream OAuth issuer via FastMCP's
`OAuthProxy`. Configuration is env-driven so the same image works across
environments.
"""

from __future__ import annotations

import os

from fastmcp.server.auth import JWTVerifier, OAuthProxy


def build_auth() -> OAuthProxy | None:
    """Return an `OAuthProxy` if all required OAuth env vars are set, else None.

    A `None` return means the caller is responsible for deciding what to do —
    the CLI aborts (fail-closed) with a friendly message. Partial
    configuration (e.g. issuer set but client id missing) also returns None
    so the caller's error message fires instead of a bare `KeyError`.
    """
    issuer = os.environ.get("SPEKOAI_OAUTH_ISSUER")
    client_id = os.environ.get("SPEKOAI_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("SPEKOAI_OAUTH_CLIENT_SECRET")
    base_url = os.environ.get("SPEKOAI_MCP_BASE_URL")
    if not (issuer and client_id and client_secret and base_url):
        return None

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

    # Default audience is the OAuth client_id (what Better Auth's
    # oauth-provider plugin mints by default). Override via
    # SPEKOAI_OAUTH_AUDIENCE if the issuer is configured to emit a
    # different `aud` (e.g. the resource URL per RFC 8707).
    audience = os.environ.get("SPEKOAI_OAUTH_AUDIENCE", client_id)

    return OAuthProxy(
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
    )
