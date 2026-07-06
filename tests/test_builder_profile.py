"""Tests for the per-request builder tool profile (`/mcp?profile=builder`).

Covers the three invariants of platform issue #1169 section 1:

1. The builder profile advertises exactly the curated preset.
2. The DEFAULT profile (no/unknown `profile` query param, or no HTTP
   request at all) is byte-identical to the pre-profile surface — same
   names, same order, and builder-only tools are neither listed nor
   callable.
3. `code_snippets.get` returns non-empty, correct-by-anchor code for
   every supported framework.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import NotFoundError

import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.builder_tools import BUILDER_TOOL_NAMES
from spekoai_mcp.code_snippets import SNIPPET_FRAMEWORKS
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import (
    BUILDER_ONLY_TOOL_NAMES,
    BUILDER_PROFILE_TOOL_NAMES,
)
from spekoai_mcp.server import create_server

DEFAULT_TOOL_NAMES = ACTION_TOOL_NAMES + DOCS_TOOL_NAMES


def _force_http_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    """Simulate an HTTP request whose query string carries `profile`."""
    query_params: dict[str, str] = {} if profile is None else {"profile": profile}
    fake_request = SimpleNamespace(query_params=query_params)
    monkeypatch.setattr(profiles, "get_http_request", lambda: fake_request)


# --- profile constants stay coherent ---------------------------------------


def test_builder_preset_is_composed_of_known_tools() -> None:
    known = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    assert set(BUILDER_PROFILE_TOOL_NAMES) <= known
    assert BUILDER_ONLY_TOOL_NAMES == set(BUILDER_TOOL_NAMES)
    assert BUILDER_ONLY_TOOL_NAMES <= set(BUILDER_PROFILE_TOOL_NAMES)
    # Builder-only tools must not collide with the default surface.
    assert not BUILDER_ONLY_TOOL_NAMES & set(DEFAULT_TOOL_NAMES)


# --- default profile: byte-identical ----------------------------------------


async def test_default_profile_without_http_request_is_unchanged() -> None:
    """Outside HTTP (stdio, in-process) the default surface applies."""
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == DEFAULT_TOOL_NAMES


async def test_default_profile_over_http_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, None)
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == DEFAULT_TOOL_NAMES


async def test_unknown_profile_value_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "ops")
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == DEFAULT_TOOL_NAMES


async def test_builder_only_tools_not_callable_on_default_profile() -> None:
    mcp = create_server()
    for name in sorted(BUILDER_ONLY_TOOL_NAMES):
        with pytest.raises(NotFoundError, match=f"Unknown tool: '{name}'"):
            await mcp.call_tool(name, {"framework": "curl"})


# --- builder profile ---------------------------------------------------------


async def test_builder_profile_lists_exactly_the_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == BUILDER_PROFILE_TOOL_NAMES


async def test_builder_profile_tools_expose_quality_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    tools = await create_server().list_tools()
    assert all(tool.title for tool in tools)
    assert all(tool.output_schema for tool in tools)
    assert all(tool.output_schema["type"] == "object" for tool in tools)
    assert all(tool.annotations is not None for tool in tools)

    by_name = {tool.name: tool for tool in tools}
    assert by_name["code_snippets.get"].annotations.readOnlyHint is True
    assert by_name["voices.list"].annotations.readOnlyHint is True
    assert by_name["models.list"].annotations.readOnlyHint is True
    assert by_name["agents.create"].annotations.readOnlyHint is False


async def test_builder_profile_blocks_tools_outside_the_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    mcp = create_server()
    for name in ["agents.delete", "sessions.list", "phone_numbers.create"]:
        assert name not in BUILDER_PROFILE_TOOL_NAMES
        with pytest.raises(NotFoundError, match=f"Unknown tool: '{name}'"):
            await mcp.call_tool(name, {})


# --- code_snippets.get -------------------------------------------------------


async def test_get_code_snippet_returns_code_for_every_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    mcp = create_server()
    for framework in SNIPPET_FRAMEWORKS:
        result = await mcp.call_tool("code_snippets.get", {"framework": framework})
        payload = result.structured_content or {}
        assert payload["framework"] == framework
        assert isinstance(payload["code"], str) and payload["code"].strip()
        assert payload["notes"]
        assert payload["docs_resources"]
        # The ready-to-paste code is also the text content.
        assert result.content[0].text == payload["code"]


async def test_code_snippets_carry_correctness_anchors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anchor the snippets to the real integration surface: the session
    mint endpoint, the secret key env var, and the browser SDK entrypoint
    with its short-lived credential fields."""
    _force_http_profile(monkeypatch, "builder")
    mcp = create_server()

    async def code_for(framework: str) -> str:
        result = await mcp.call_tool("code_snippets.get", {"framework": framework})
        return (result.structured_content or {})["code"]

    for framework in SNIPPET_FRAMEWORKS:
        code = await code_for(framework)
        assert "transportToken" in code
        assert "transportUrl" in code

    # Server-side snippets mint the session against the real endpoint with
    # the secret key. The react snippet is deliberately browser-only (the
    # API key must never reach browser code), so it is excluded here.
    for framework in ("nextjs", "node", "python", "curl"):
        code = await code_for(framework)
        assert "/v1/sessions" in code
        assert "SPEKO_API_KEY" in code

    for framework in ("nextjs", "react"):
        code = await code_for(framework)
        assert "@spekoai/client" in code
        assert "VoiceConversation.create" in code
        assert "endSession" in code
        # Transcript rendering must use the SDK-reconciled onTranscript
        # feed; appending raw onMessage events duplicates segments
        # (they re-deliver cumulatively per packages/client/src/types.ts).
        assert "onTranscript" in code
        assert "onMessage:" not in code


async def test_get_code_snippet_rejects_unknown_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    with pytest.raises(Exception, match="framework"):
        await create_server().call_tool("code_snippets.get", {"framework": "ruby"})


# --- voices.list / models.list relay -----------------------------------------


def _capture_speko_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(
        http_client,
        "get_access_token",
        lambda: SimpleNamespace(token="upstream-oauth-token"),
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""
        reason_phrase = "OK"
        content = b"{}"

        def json(self) -> dict[str, object]:
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool, transport: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            json: object,
        ) -> FakeResponse:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(http_client.httpx, "AsyncClient", FakeAsyncClient)
    return captured


async def test_list_voices_relays_to_voices_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    captured = _capture_speko_api(monkeypatch)
    result = await create_server().call_tool("voices.list", {"provider": "cartesia"})
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.speko.dev/v1/voices?provider=cartesia"
    assert captured["headers"] == {"Authorization": "Bearer upstream-oauth-token"}
    assert result.structured_content == {"ok": True}


async def test_list_models_relays_to_providers_known_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    captured = _capture_speko_api(monkeypatch)
    result = await create_server().call_tool("models.list", {})
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.speko.dev/v1/providers/known"
    assert result.structured_content == {"ok": True}
