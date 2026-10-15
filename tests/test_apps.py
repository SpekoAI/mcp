"""The bundled MCP App that makes `audio.synthesize` audible."""

from __future__ import annotations

import re

import pytest
from fastmcp.apps import UI_MIME_TYPE

from spekoai_mcp.apps import (
    AUDIO_PLAYER_URI,
    BASE_URL_ENV_VAR,
    OPENAI_INVOKED_META_KEY,
    OPENAI_INVOKING_META_KEY,
    OPENAI_TEMPLATE_META_KEY,
    OPENAI_TOOL_INVOCATION_META_KEY,
    OPENAI_WIDGET_DESCRIPTION_META_KEY,
    OPENAI_WIDGET_DOMAIN_META_KEY,
)
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


async def test_synthesize_tool_carries_the_chatgpt_alias() -> None:
    """ChatGPT reads its own key, whatever its docs say about the standard one.

    Measured 2026-09-16: `openai-mcp/1.0.0 (Codex)` called the tool against
    `chatgpt.speko.ai` and never issued the `resources/read` that rendering
    needs. Both keys point at the same resource, so they cannot drift.
    """
    mcp = create_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["audio.synthesize"].meta or {}
    assert meta.get(OPENAI_TEMPLATE_META_KEY) == AUDIO_PLAYER_URI
    assert meta[OPENAI_TEMPLATE_META_KEY] == (meta.get("ui") or {}).get("resourceUri")


async def test_other_action_tools_carry_no_app() -> None:
    """Binding is opt-in per tool, so a JSON payload keeps its text rendering."""
    mcp = create_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["audio.transcribe"].meta or {}
    assert "ui" not in meta
    assert OPENAI_TEMPLATE_META_KEY not in meta


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


async def _player_resource_meta() -> tuple[dict, dict]:
    """The player's `_meta` as `resources/list` and `resources/read` serve it."""
    mcp = create_server()
    resources = {str(resource.uri): resource for resource in await mcp.list_resources()}
    listed = resources[AUDIO_PLAYER_URI].get_meta()
    result = await mcp.read_resource(AUDIO_PLAYER_URI)
    read = result.contents[0].meta or {}
    return listed, read


async def test_player_declares_the_deployment_origin_for_chatgpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ChatGPT will not accept an app with UI for submission without a domain.

    Each profile is its own service on its own host, so the deployment's
    origin is unique to that app. Path and trailing slash are stripped: the
    value is an origin, not a URL.
    """
    monkeypatch.setenv(BASE_URL_ENV_VAR, "https://mcp.speko.ai/")
    for meta in await _player_resource_meta():
        assert meta[OPENAI_WIDGET_DOMAIN_META_KEY] == "https://mcp.speko.ai"
        assert meta[OPENAI_WIDGET_DESCRIPTION_META_KEY]


async def test_player_omits_the_domain_without_a_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local or API-key-only run has no public origin to claim."""
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    for meta in await _player_resource_meta():
        assert OPENAI_WIDGET_DOMAIN_META_KEY not in meta
        assert meta[OPENAI_WIDGET_DESCRIPTION_META_KEY]


@pytest.mark.parametrize("base_url", [None, "https://mcp.speko.ai"])
async def test_player_never_sets_the_standard_ui_domain(
    monkeypatch: pytest.MonkeyPatch, base_url: str | None
) -> None:
    """Claude refuses to render the app unless `_meta.ui.domain` is its own
    `{hash}.claudemcpcontent.com` value, so an HTTPS origin there would trade
    the ChatGPT fix for a broken player in Claude.
    """
    if base_url is None:
        monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(BASE_URL_ENV_VAR, base_url)
    for meta in await _player_resource_meta():
        assert "domain" not in (meta.get("ui") or {})


async def test_synthesize_tool_carries_chatgpt_status_text() -> None:
    """ChatGPT shows these while the tool runs and after; it caps each at 64."""
    mcp = create_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["audio.synthesize"].meta or {}
    for key in (OPENAI_INVOKING_META_KEY, OPENAI_INVOKED_META_KEY):
        assert isinstance(meta.get(key), str)
        assert 1 <= len(meta[key]) <= 64
    # The nested spelling Manufact's checklist describes, with the same text.
    assert meta[OPENAI_TOOL_INVOCATION_META_KEY] == {
        "invoking": meta[OPENAI_INVOKING_META_KEY],
        "invoked": meta[OPENAI_INVOKED_META_KEY],
    }
    other = tools["audio.transcribe"].meta or {}
    assert OPENAI_INVOKING_META_KEY not in other
    assert OPENAI_INVOKED_META_KEY not in other
    assert OPENAI_TOOL_INVOCATION_META_KEY not in other
