"""Authenticated relay to the Speko router control plane.

Mirrors `http_client` — same error types, same "forward the caller's own
credential, hold none of our own" property — against a different service:
the router control plane at `control.speko.ai`, which owns router API keys
and the routing policy carried on them. `http_client` talks to the Agents
API at `api.speko.dev`. Two services, two auth authorities, so the base URL
is a separate env var (`SPEKOAI_ROUTER_CONTROL_URL`) rather than a path on
`get_api_base()`.

One rule this module adds on top of `http_client`: a Speko API key (`sk_*`)
may NOT provision router keys. `requireUserPrincipal` in
`apps/server/src/middleware/org-access.ts` draws exactly that line on the
Agents side ("API keys cannot manage API keys; sign in to the dashboard as
an org member") — a leaked inference credential must not escalate into a
key factory. The control plane enforces it independently (it resolves a
canonical user, never a machine principal); the check here fails fast with
an actionable message and keeps the key off the wire entirely.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp.server.dependencies import get_access_token

from spekoai_mcp.http_client import (
    SpekoApiError,
    SpekoAuthError,
)
from spekoai_mcp.http_client import (
    _error_details as error_details,  # shared response-error parsing
)

DEFAULT_CONTROL_BASE = "https://control.speko.ai"

API_KEY_REFUSAL = (
    "Router keys are provisioned by a signed-in user. Reconnect Speko MCP with "
    "OAuth; a Speko API key cannot mint router keys."
)

_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


def get_control_base() -> str:
    return (os.environ.get("SPEKOAI_ROUTER_CONTROL_URL") or DEFAULT_CONTROL_BASE).rstrip("/")


def _control_bearer_token() -> str:
    """The caller's own OAuth access token, or a refusal.

    Deliberately NOT `http_client._bearer_token()`: this surface accepts
    one credential class, not two.
    """
    access_token = get_access_token()
    if access_token is None:
        raise SpekoAuthError(
            "This tool requires the authenticated SpekoAI MCP endpoint. "
            "Connect /mcp with OAuth."
        )
    token = getattr(access_token, "token", access_token)
    if not isinstance(token, str) or not token:
        raise SpekoAuthError("Authenticated MCP token is missing or invalid.")
    scopes = getattr(access_token, "scopes", None) or []
    if token.startswith("sk_") or "api_key" in scopes:
        raise SpekoAuthError(API_KEY_REFUSAL)
    return token


async def call_control_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the router control plane with the caller's OAuth token.

    Returns `{}` for a 204 (the control plane's success shape for
    `DELETE /api/keys/:id`).
    """
    control_base = get_control_base()
    url = f"{control_base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers={"Authorization": f"Bearer {_control_bearer_token()}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(
            0, f"Unable to reach the Speko router control plane at {control_base}: {exc}"
        ) from exc
    if resp.status_code >= 400:
        message, trace_id = error_details(resp)
        raise SpekoApiError(resp.status_code, message, trace_id=trace_id)
    if not resp.content:
        return {}
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SpekoApiError(
            resp.status_code, "The Speko router control plane returned a non-JSON response."
        ) from exc
    if not isinstance(payload, dict):
        raise SpekoApiError(
            resp.status_code, "The Speko router control plane returned an unexpected JSON response."
        )
    return payload
