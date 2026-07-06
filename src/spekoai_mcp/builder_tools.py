"""Builder-profile-only tools for the hosted MCP server.

These tools exist for app builders (v0, Lovable, Bolt, Replit, Base44,
Figma Make) whose agents consume MCP tools during code generation. They
are registered on the shared server but only advertised (and callable)
under ``/mcp?profile=builder`` — ``ToolProfileMiddleware`` in
``profiles.py`` hides them from the default surface so existing clients
see zero change.

- ``voices.list``  — relay of GET /v1/voices (TTS voice + provider catalog).
- ``models.list``  — relay of GET /v1/providers/known (STT/LLM/TTS/S2S
  provider+model catalog with ``allowedProviders`` pin ids).
- ``code_snippets.get`` — local, returns ready-to-paste Speko integration
  code (web voice call + server-side session mint) per framework, sourced
  from the bundled SDK docs. See ``code_snippets.py``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from spekoai_mcp import http_client
from spekoai_mcp.action_tools import SPEKO_API_OUTPUT_SCHEMA, call, tool_title
from spekoai_mcp.code_snippets import SNIPPET_FRAMEWORKS, SnippetFramework, get_snippet

BUILDER_TOOL_NAME_BY_FUNCTION = {
    "list_voices": "voices.list",
    "list_models": "models.list",
    "get_code_snippet": "code_snippets.get",
}

BUILDER_TOOL_NAMES = list(BUILDER_TOOL_NAME_BY_FUNCTION.values())

CODE_SNIPPET_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Ready-to-paste Speko integration code for one framework.",
    "properties": {
        "framework": {
            "type": "string",
            "description": "Requested framework.",
            "enum": list(SNIPPET_FRAMEWORKS),
        },
        "title": {"type": "string", "description": "One-line snippet summary."},
        "language": {
            "type": "string",
            "description": "Syntax hint for the code body: tsx, js, python, bash.",
        },
        "code": {
            "type": "string",
            "description": "The full snippet body, ready to paste into the app.",
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Integration rules the generated app must follow.",
        },
        "docs_resources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "spekoai://docs/... resources with the full API surface.",
        },
    },
    "required": ["framework", "title", "language", "code", "notes", "docs_resources"],
    "additionalProperties": False,
}


def register_builder_tools(mcp: FastMCP) -> None:
    """Register builder-profile tools. MUST run after the default-surface
    registrations so the default tool ordering stays byte-identical (the
    profile middleware filters these out of the default view)."""
    for tool in [
        list_voices,
        list_models,
        get_code_snippet,
    ]:
        name = tool.__name__
        public_name = BUILDER_TOOL_NAME_BY_FUNCTION[name]
        title = tool_title(name)
        mcp.tool(
            tool,
            name=public_name,
            title=title,
            output_schema=(
                CODE_SNIPPET_OUTPUT_SCHEMA
                if name == "get_code_snippet"
                else SPEKO_API_OUTPUT_SCHEMA
            ),
            annotations=ToolAnnotations(
                title=title,
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=name != "get_code_snippet",
            ),
        )


async def list_voices(
    provider: Annotated[
        str | None,
        Field(
            description=(
                "Optional TTS provider filter, e.g. 'cartesia', 'elevenlabs', "
                "'openai', 'inworld'. Omit to list every voice."
            )
        ),
    ] = None,
) -> ToolResult:
    """List the Speko TTS voice catalog: voices (vendor, id, name) plus TTS
    providers with their models. Use a returned voice id as the `voice`
    field on agents.create or POST /v1/sessions bodies."""
    return await call(
        "GET",
        http_client.with_query("/v1/voices", {"provider": provider}),
        text="Retrieved voice catalog.",
    )


async def list_models() -> ToolResult:
    """List the STT/LLM/TTS/S2S provider and model catalog. Each entry's
    `id` ('vendor' or 'vendor:model') is the literal string accepted by
    `allowedProviders` pins in agent and session configs; `benchmarked`
    marks entries with live Speko benchmark scores."""
    return await call("GET", "/v1/providers/known", text="Retrieved model catalog.")


async def get_code_snippet(
    framework: Annotated[
        SnippetFramework,
        Field(
            description=(
                "Target framework for the integration snippet: 'nextjs' "
                "(App Router route handler + client page), 'react' (browser "
                "component for any SPA), 'node' (Express session-mint "
                "endpoint), 'python' (FastAPI session-mint endpoint), or "
                "'curl' (raw HTTP)."
            )
        ),
    ],
) -> ToolResult:
    """Get ready-to-paste Speko integration code for a web voice call.

    Returns correct, compilable code for the canonical Speko runtime flow:
    the app's server mints a session via POST /v1/sessions with the secret
    SPEKO_API_KEY, then the browser connects with @spekoai/client's
    VoiceConversation.create using the returned short-lived transportToken
    and transportUrl. Use this INSTEAD of guessing Speko API shapes when
    generating app code. Note: generated apps cannot call MCP tools at
    runtime - runtime integration is exactly this code plus a SPEKO_API_KEY
    environment variable."""
    payload = get_snippet(framework)
    return ToolResult(
        content=[TextContent(type="text", text=str(payload["code"]))],
        structured_content=payload,
    )
