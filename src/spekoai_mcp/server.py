"""FastMCP v3 server exposing the SpekoAI SDK as MCP tools.

Tool surface mirrors `spekoai.AsyncSpeko` exactly — do not invent surface
the SDK lacks. When the SDK grows, extend here.

Auth model: every tool call forwards the caller's OAuth access token to
the SpekoAI API. No server-wide SpekoAI credential exists — the JWT from
`OAuthProxy` *is* the credential the upstream API validates.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.dependencies import get_access_token
from pydantic import Field
from spekoai import AsyncSpeko
from spekoai.models import UsageSummary
from starlette.requests import Request
from starlette.responses import PlainTextResponse


def _base_url() -> str:
    return os.environ.get("SPEKOAI_BASE_URL", "https://api.speko.ai")


def _caller_client() -> AsyncSpeko:
    """Build an SDK client scoped to the MCP caller's OAuth token.

    Raises `ToolError` if no token is present — shouldn't happen under
    `OAuthProxy`, which rejects unauthenticated requests before tools run,
    but we guard rather than silently call upstream with no credential.
    """
    token = get_access_token()
    if token is None or not token.token:
        raise ToolError("No OAuth access token on request; cannot call SpekoAI API.")
    return AsyncSpeko(api_key=token.token, base_url=_base_url())


def create_server(auth: OAuthProxy | None = None) -> FastMCP:
    """Build a FastMCP server. `auth` is passed via the constructor (the
    supported wiring path); production deployments must supply one.
    """
    mcp: FastMCP = FastMCP(
        name="spekoai",
        instructions=(
            "SpekoAI voice-AI gateway. Tools mirror the SpekoAI SDK surface "
            "and act on behalf of the authenticated caller."
        ),
        auth=auth,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @mcp.tool
    async def get_usage_summary(
        _ctx: Context,
        from_date: Annotated[
            str | None,
            Field(
                description=(
                    "ISO-8601 start date (inclusive), e.g. '2026-04-01' or "
                    "'2026-04-01T00:00:00Z'. Defaults to the start of the "
                    "current billing period."
                ),
            ),
        ] = None,
        to_date: Annotated[
            str | None,
            Field(
                description=(
                    "ISO-8601 end date (inclusive), e.g. '2026-04-14'. "
                    "Defaults to now."
                ),
            ),
        ] = None,
    ) -> UsageSummary:
        """Get usage summary for the current billing period."""
        async with _caller_client() as client:
            return await client.usage.get(from_date=from_date, to_date=to_date)

    return mcp
