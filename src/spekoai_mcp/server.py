"""FastMCP v3 server exposing the SpekoAI SDK as MCP tools.

Tool surface mirrors `spekoai.AsyncSpekoAI` exactly — do not invent surface
the SDK lacks. When the SDK grows, extend here.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastmcp import Context, FastMCP
from fastmcp.server.auth import OAuthProxy
from pydantic import Field
from spekoai import AsyncSpekoAI
from spekoai.models import UsageSummary
from starlette.requests import Request
from starlette.responses import PlainTextResponse


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[dict[str, AsyncSpekoAI]]:
    """Construct the SDK client bound to the running event loop and tear it
    down on shutdown. Exposed to tools via `ctx.request_context.lifespan_context`.
    """
    async with AsyncSpekoAI(
        api_key=os.environ["SPEKOAI_API_KEY"],
        base_url=os.environ.get("SPEKOAI_BASE_URL", "https://api.speko.ai"),
    ) as client:
        yield {"spekoai_client": client}


def create_server(auth: OAuthProxy | None = None) -> FastMCP:
    """Build a FastMCP server. `auth` is passed via the constructor (the
    supported wiring path); omit for stdio/local development.
    """
    mcp: FastMCP = FastMCP(
        name="spekoai",
        instructions=(
            "SpekoAI voice-AI gateway. Use these tools to inspect usage across "
            "the STT→LLM→TTS voice pipelines proxied through the gateway."
        ),
        lifespan=_lifespan,
        auth=auth,
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @mcp.tool
    async def get_usage_summary(
        ctx: Context,
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
        client: AsyncSpekoAI = ctx.request_context.lifespan_context["spekoai_client"]
        return await client.usage.get(from_date=from_date, to_date=to_date)

    return mcp
