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

    # RFC 8707 resource indicator for this MCP deployment. Better Auth's
    # oauth-provider only mints a JWT (rather than an opaque random
    # string) when the authorize/token request carries a `resource`
    # parameter that matches its `validAudiences` allowlist. We inject
    # one below via `extra_authorize_params` / `extra_token_params`, and
    # the JWT's `aud` claim then matches this value.
    #
    # Default matches FastMCP's own resource URL (`{base_url}/mcp`),
    # which FastMCP normalizes via `base_url.rstrip("/") + "/" + path`
    # (see `fastmcp/server/auth/auth.py::_get_resource_url`). Strip a
    # trailing slash on `base_url` here too — otherwise a
    # `SPEKOAI_MCP_BASE_URL=https://host/` produces `//mcp` and the
    # `aud` claim stops matching FastMCP's advertised resource URL.
    # Override via SPEKOAI_OAUTH_AUDIENCE if the MCP endpoint is mounted
    # at a non-default path.
    audience = os.environ.get("SPEKOAI_OAUTH_AUDIENCE", f"{base_url.rstrip('/')}/mcp")

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
        # Force the upstream to mint a JWT access token by sending
        # `resource` on both legs of the auth-code flow. Without this
        # the Inspector (and other clients that don't send `resource`
        # themselves) causes Better Auth to emit an opaque token that
        # JWTVerifier can't verify, which the upstream bounces as
        # `invalid_token` on every MCP request — a silent redirect loop.
        extra_authorize_params={"resource": audience},
        extra_token_params={"resource": audience},
    )
