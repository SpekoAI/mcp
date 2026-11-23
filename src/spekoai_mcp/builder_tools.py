"""Builder-profile-only tools for the hosted MCP server.

These tools exist for app builders (v0, Lovable, Bolt, Replit, Base44,
Figma Make) whose agents consume MCP tools during code generation. They
are registered on the shared image but only advertised (and callable) by
the builder deployment at ``https://builder-mcp.speko.ai/mcp``.
``ToolProfileMiddleware`` in ``profiles.py`` hides them from every other
surface.

- ``voices.list``  — relay of GET /v1/voices (TTS voice + provider catalog).
- ``models.list``  — relay of GET /v1/providers/known (STT/LLM/TTS/S2S
  provider+model catalog with ``allowedProviders`` pin ids).
- ``code_snippets.get`` — local, returns ready-to-paste Speko integration
  code (web voice call + server-side session mint) per framework, sourced
  from the bundled SDK docs. See ``code_snippets.py``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from spekoai_mcp import http_client
from spekoai_mcp.action_tools import SPEKO_API_OUTPUT_SCHEMA, call, tool_title
from spekoai_mcp.code_snippets import SNIPPET_FRAMEWORKS, SnippetFramework, get_snippet
from spekoai_mcp.profiles import current_profile, profile_serves_tool

BUILDER_TOOL_NAME_BY_FUNCTION = {
    "list_voices": "voices.list",
    "search_voices": "voices.search",
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
    descriptions = _profile_descriptions(current_profile())
    for tool in [
        list_voices,
        search_voices,
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
            description=descriptions.get(name),
            output_schema=(
                CODE_SNIPPET_OUTPUT_SCHEMA
                if name == "get_code_snippet"
                else SPEKO_API_OUTPUT_SCHEMA
            ),
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=name != "get_code_snippet",
            ),
        )


def _profile_descriptions(profile: str | None) -> dict[str, str]:
    """Descriptions that name the audio tools only where ``profile`` serves them."""
    synthesize = profile_serves_tool("audio.synthesize", profile)
    audio_tools = [
        name
        for name in ("audio.synthesize", "audio.transcribe")
        if profile_serves_tool(name, profile)
    ]
    use_with = (
        "pass `voice` as audio.synthesize's `voice` and pin the provider in "
        "constraints.allowedProviders.tts"
        if synthesize
        else "pass `voice` as an agent's `voice` and pin the provider in "
        "stackPreferences.allowedProviders.tts"
    )
    priced_scope = f" on {' and '.join(audio_tools)}," if audio_tools else ""
    return {
        "search_voices": (
            "Search every vendor's TTS voices that Speko can route, including ElevenLabs "
            "on a Speko-managed key. `name` matches part of a voice's name or id, ignoring "
            f"case. Each result carries `use_with` ({{provider, model, voice}}): {use_with}. "
            "Results are paginated; follow `next_cursor`."
        ),
        "list_models": (
            "List the STT/LLM/TTS/S2S provider and model catalog. Each entry's `id` "
            "('vendor' or 'vendor:model') is the literal string accepted by "
            "`allowedProviders` pins in agent and session configs; `benchmarked` marks "
            "entries with live Speko benchmark scores. STT and TTS entries carry "
            f"`priced`:{priced_scope} a `priced: false` model is skipped on Speko-managed "
            "keys (the request fails if no priced model is left) and works only with the "
            "caller's own provider key, so pin a `priced: true` model for managed usage."
        ),
    }


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
    field on an agent, session or synthesis body. ElevenLabs voices
    are not in this list (its provider entry has voicesFetchedLive: true);
    they are in the voice index, GET /v1/tts/voices?vendor=elevenlabs."""
    return await call(
        "GET",
        http_client.with_query("/v1/voices", {"provider": provider}),
        text="Retrieved voice catalog.",
    )


async def search_voices(
    vendor: Annotated[
        str | None,
        Field(description="TTS vendor, e.g. 'elevenlabs', 'cartesia', 'minimax'."),
    ] = None,
    language: Annotated[
        str | None,
        Field(description="Language code the voice is native to, e.g. 'es' or 'en-GB'."),
    ] = None,
    gender: Annotated[
        Literal["male", "female", "neutral"] | None,
        Field(description="Voice gender."),
    ] = None,
    accent: Annotated[str | None, Field(description="Accent, e.g. 'mexican'.")] = None,
    name: Annotated[
        str | None,
        Field(description="Part of the voice's name or id, e.g. 'Charon'. Case does not matter."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Page size.")] = 25,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
) -> ToolResult:
    """Search the TTS voices Speko can route. The registered description is
    `_profile_descriptions`."""
    return await call(
        "GET",
        http_client.with_query(
            "/v1/tts/voices",
            {
                "vendor": vendor,
                "language": language,
                "gender": gender,
                "accent": accent,
                "name": name,
                "limit": limit,
                "cursor": cursor,
            },
        ),
        text="Retrieved matching voices.",
    )


async def list_models() -> ToolResult:
    """List the provider and model catalog. The registered description is
    `_profile_descriptions`."""
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
