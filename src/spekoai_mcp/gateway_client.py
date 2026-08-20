"""Authenticated relay to Runtime's organization-owned Gateway key API."""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastmcp.server.dependencies import get_access_token

from spekoai_mcp.http_client import SpekoApiError, SpekoAuthError
from spekoai_mcp.http_client import _error_details as error_details

DEFAULT_GATEWAY_URL = "https://gateway.speko.dev"
GATEWAY_KEY_SCOPE = "gateway.keys.manage"

_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


def get_gateway_url() -> str:
    return (os.environ.get("SPEKOAI_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/")


def gateway_bearer_token() -> str:
    access_token = get_access_token()
    if access_token is None:
        raise SpekoAuthError("Connect /mcp with a Speko API key.")
    token = getattr(access_token, "token", access_token)
    scopes = getattr(access_token, "scopes", None) or []
    if not isinstance(token, str) or not token.startswith("sk_"):
        raise SpekoAuthError("The authenticated MCP credential is not a Speko API key.")
    if GATEWAY_KEY_SCOPE not in scopes:
        raise SpekoAuthError(
            "This API key is missing gateway.keys.manage. An organization owner "
            "or admin must create a key with Manage Gateway API keys enabled."
        )
    return token


async def call_gateway_api(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gateway_url = get_gateway_url()
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=False,
            transport=_TEST_TRANSPORT,
        ) as client:
            response = await client.request(
                method.upper(),
                f"{gateway_url}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {gateway_bearer_token()}"},
                json=body,
            )
    except httpx.HTTPError as exc:
        raise SpekoApiError(0, f"Unable to reach Speko Gateway at {gateway_url}: {exc}") from exc

    if response.status_code >= 400:
        message, trace_id = error_details(response)
        raise SpekoApiError(response.status_code, message, trace_id=trace_id)
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpekoApiError(
            response.status_code, "Speko Gateway returned a non-JSON response."
        ) from exc
    if not isinstance(payload, dict):
        raise SpekoApiError(response.status_code, "Speko Gateway returned unexpected JSON.")
    return payload
