"""The bundled MCP App that makes `audio.synthesize` audible."""

from __future__ import annotations

import re

from fastmcp.apps import UI_MIME_TYPE

from spekoai_mcp.apps import AUDIO_PLAYER_URI
from spekoai_mcp.server import create_public_server, create_server


async def test_audio_player_resource_is_advertised() -> None:
    mcp = create_server()
    resources = {str(resource.uri): resource for resource in await mcp.list_resources()}
    assert AUDIO_PLAYER_URI in resources
    # The host only renders a resource served under the apps profile; a plain
    # `text/html` body is an ordinary document and is never framed.
    assert resources[AUDIO_PLAYER_URI].mime_type == UI_MIME_TYPE


async def test_audio_player_not_on_the_public_docs_surface() -> None:
    """The unauthenticated docs server has no `audio.synthesize` to render."""
    mcp = create_public_server()
    uris = [str(resource.uri) for resource in await mcp.list_resources()]
    assert AUDIO_PLAYER_URI not in uris


async def test_synthesize_tool_points_at_the_player() -> None:
    mcp = create_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["audio.synthesize"].meta or {}
    assert (meta.get("ui") or {}).get("resourceUri") == AUDIO_PLAYER_URI


async def test_other_action_tools_carry_no_app() -> None:
    """Binding is opt-in per tool, so a JSON payload keeps its text rendering."""
    mcp = create_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["audio.transcribe"].meta or {}
    assert "ui" not in meta


async def test_player_html_loads_nothing_from_the_network() -> None:
    """Self-contained HTML is why the resource declares no CSP domains.

    A `src`/`href` added later would be blocked by the host's default CSP and
    the widget would fail in the iframe but pass every test above, so guard the
    property rather than the metadata.
    """
    mcp = create_server()
    result = await mcp.read_resource(AUDIO_PLAYER_URI)
    html = result.contents[0].content
    assert isinstance(html, str)
    assert not re.search(r"""<script[^>]+\bsrc\s*=""", html)
    assert not re.search(r"""<link[^>]+\bhref\s*=""", html)
    assert "//unpkg.com" not in html
