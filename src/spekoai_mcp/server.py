"""FastMCP v3 server exposing the SpekoAI SDK as MCP tools.

Tool surface mirrors `spekoai.AsyncSpekoAI` exactly — do not invent surface
the SDK lacks. When the SDK grows, extend here.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import AsyncIterator, Optional

from fastmcp import FastMCP
from spekoai import AsyncSpekoAI
from spekoai.models import UsageSummary
from starlette.requests import Request
from starlette.responses import PlainTextResponse


@lru_cache(maxsize=1)
def _get_client() -> AsyncSpekoAI:
    """Lazily construct the SDK client so importing the module doesn't require env vars."""
    return AsyncSpekoAI(
        api_key=os.environ["SPEKOAI_API_KEY"],
        base_url=os.environ.get("SPEKOAI_BASE_URL", "https://api.speko.ai"),
    )


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if _get_client.cache_info().currsize:
            await _get_client().close()
            _get_client.cache_clear()


mcp: FastMCP = FastMCP(
    name="spekoai",
    instructions=(
        "SpekoAI voice-AI gateway. Use these tools to inspect usage across "
        "the STT→LLM→TTS voice pipelines proxied through the gateway."
    ),
    lifespan=_lifespan,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.tool
async def get_usage_summary(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> UsageSummary:
    """Get usage summary for the current billing period.

    Dates are ISO-8601 strings. Both arguments are optional.
    """
    return await _get_client().usage.get(from_date=from_date, to_date=to_date)
