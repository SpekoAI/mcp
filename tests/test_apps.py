"""The bundled MCP App that makes `audio.synthesize` audible."""

from __future__ import annotations

import re

import pytest
from fastmcp.apps import UI_MIME_TYPE

from spekoai_mcp.apps import (
    AUDIO_PLAYER_URI,
    BASE_URL_ENV_VAR,
    CONNECT_URI,
    OPENAI_INVOKED_META_KEY,
    OPENAI_INVOKING_META_KEY,
    OPENAI_TEMPLATE_META_KEY,
    OPENAI_TOOL_INVOCATION_META_KEY,
    OPENAI_WIDGET_DESCRIPTION_META_KEY,
    OPENAI_WIDGET_DOMAIN_META_KEY,
)
from spekoai_mcp.server import create_public_server, create_server
from spekoai_mcp.setup_tools import MCP_ENDPOINT


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


# --- the public endpoint's setup card ------------------------------------
# One test per MCP Apps publishing check, so the public docs endpoint's view
# keeps passing all of them.


async def test_connect_card_is_public_only() -> None:
    """The card is for people who have not connected yet; `/mcp` never lists it."""
    public = {str(resource.uri) for resource in await create_public_server().list_resources()}
    operational = {str(resource.uri) for resource in await create_server().list_resources()}
    assert CONNECT_URI in public
    assert CONNECT_URI not in operational
    assert "setup.connect" not in {tool.name for tool in await create_server().list_tools()}


async def test_connect_card_envelope_is_valid() -> None:
    """View resource envelope: a `ui://` URI read back as the MCP Apps HTML type."""
    mcp = create_public_server()
    resources = {str(resource.uri): resource for resource in await mcp.list_resources()}
    assert resources[CONNECT_URI].mime_type == UI_MIME_TYPE
    content = (await mcp.read_resource(CONNECT_URI)).contents[0]
    assert content.mime_type == UI_MIME_TYPE
    assert isinstance(content.content, str)
    assert content.content.lstrip().startswith("<!doctype html>")


async def test_setup_tool_links_to_the_card() -> None:
    """Tool-linked resource URI is readable, and both spellings point at it."""
    mcp = create_public_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    meta = tools["setup.connect"].meta or {}
    ui = meta.get("ui") or {}
    assert ui.get("resourceUri") == CONNECT_URI
    assert meta.get(OPENAI_TEMPLATE_META_KEY) == CONNECT_URI
    # Visibility stays unset, so the tool remains callable by the model.
    assert "visibility" not in ui
    assert (await mcp.read_resource(CONNECT_URI)).contents


async def test_setup_tool_carries_invocation_status_text() -> None:
    """Tool invocation metadata: flat keys plus the nested object, each ≤ 64."""
    tools = {tool.name: tool for tool in await create_public_server().list_tools()}
    meta = tools["setup.connect"].meta or {}
    for key in (OPENAI_INVOKING_META_KEY, OPENAI_INVOKED_META_KEY):
        assert isinstance(meta.get(key), str)
        assert 1 <= len(meta[key]) <= 64
    assert meta[OPENAI_TOOL_INVOCATION_META_KEY] == {
        "invoking": meta[OPENAI_INVOKING_META_KEY],
        "invoked": meta[OPENAI_INVOKED_META_KEY],
    }


@pytest.mark.parametrize("base_url", [None, "https://mcp.speko.ai/"])
async def test_connect_card_metadata_is_complete(
    monkeypatch: pytest.MonkeyPatch, base_url: str | None
) -> None:
    """View metadata complete and domain declared, on list and read alike, and
    never `ui.domain`, which Claude only accepts as its own hashed value."""
    if base_url is None:
        monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(BASE_URL_ENV_VAR, base_url)
    mcp = create_public_server()
    resources = {str(resource.uri): resource for resource in await mcp.list_resources()}
    listed = resources[CONNECT_URI].get_meta()
    read = (await mcp.read_resource(CONNECT_URI)).contents[0].meta or {}
    for meta in (listed, read):
        assert meta[OPENAI_WIDGET_DESCRIPTION_META_KEY]
        assert "domain" not in (meta.get("ui") or {})
        if base_url is None:
            assert OPENAI_WIDGET_DOMAIN_META_KEY not in meta
        else:
            assert meta[OPENAI_WIDGET_DOMAIN_META_KEY] == "https://mcp.speko.ai"


async def test_connect_card_loads_nothing_from_the_network() -> None:
    """CSP coverage: with no literal external origin in the HTML there is
    nothing a CSP grant would have to cover. Every URL arrives in the tool's
    structured result instead."""
    html = (await create_public_server().read_resource(CONNECT_URI)).contents[0].content
    assert isinstance(html, str)
    assert not re.search(r"https?://", html)
    assert not re.search(r"""<script[^>]+\bsrc\s*=""", html)
    assert not re.search(r"""<link[^>]+\bhref\s*=""", html)


async def test_connect_card_follows_the_host_handshake() -> None:
    """Initializes, acknowledges, renders tool results, and reports its size so
    a host never frames it at zero height."""
    html = (await create_public_server().read_resource(CONNECT_URI)).contents[0].content
    for method in (
        "ui/initialize",
        "ui/notifications/initialized",
        "ui/notifications/tool-result",
        "ui/notifications/size-changed",
        "ui/open-link",
    ):
        assert method in html


async def test_setup_connect_returns_the_steps() -> None:
    mcp = create_public_server()
    result = await mcp.call_tool("setup.connect", {})
    structured = result.structured_content
    assert structured is not None
    assert structured["endpoint"] == MCP_ENDPOINT
    assert [entry["id"] for entry in structured["clients"]] == [
        "claude-code",
        "codex",
        "opencode",
        "cursor",
        "other",
    ]
    assert all(MCP_ENDPOINT in entry["snippet"] for entry in structured["clients"])
    # A host without MCP Apps gets the steps themselves, not an acknowledgment.
    text = result.content[0].text
    assert MCP_ENDPOINT in text
    assert structured["installer"] in text
    assert all(entry["snippet"] in text for entry in structured["clients"])
