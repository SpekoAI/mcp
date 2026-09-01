"""The text block must carry the payload, on every tool, on every profile.

The bug this file exists to prevent: tool results carried the payload only in
`structuredContent` and an acknowledgment (`Executed agents.list.`) in the text
block. Claude Code and every unit test read `structuredContent`, so the suite
was green while claude.ai web — which feeds the model text blocks only — showed
users nothing at all for every read tool on the published connector.

The sweep below is deliberately not a per-tool assertion: the regression was
introduced by one shared return path and would come back the same way.
"""

from __future__ import annotations

import json

import pytest

from spekoai_mcp import action_tools, http_client
from spekoai_mcp.action_manifest import action_entries
from spekoai_mcp.generated_action_tools import ManifestActionTool
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server
from spekoai_mcp.tool_text import TEXT_CHAR_CEILING, payload_text

SENTINEL = "sentinel-payload-2f91c4"
PAYLOAD = {"kind": "result", "data": {"probe": SENTINEL, "items": [{"id": "a1"}]}}

# Payloads that must never be rendered into the text block. Keep this list
# short and justified — every entry is a tool whose result the model cannot use
# as text and whose size would evict the conversation from the context window.
TEXT_EXEMPT_TOOLS = {"audio.synthesize"}


def test_payload_text_round_trips() -> None:
    assert json.loads(payload_text(PAYLOAD)) == PAYLOAD


def test_payload_text_truncates_with_an_explicit_marker() -> None:
    text = payload_text({"blob": "x" * (TEXT_CHAR_CEILING + 500)})
    assert len(text) < TEXT_CHAR_CEILING + 200
    assert "truncated by the Speko MCP server" in text


def test_payload_text_never_raises_on_an_unserializable_payload() -> None:
    circular: dict = {}
    circular["self"] = circular
    assert "agents.get" in payload_text(circular, action="agents.get")


def test_result_helper_carries_the_payload_not_the_acknowledgment() -> None:
    res = action_tools.result(PAYLOAD, text="Retrieved sessions.")
    assert json.loads(res.content[0].text) == PAYLOAD


def test_list_result_helper_carries_the_payload() -> None:
    res = action_tools.list_result([{"id": "a1"}], text="Retrieved sessions.")
    assert json.loads(res.content[0].text) == {"result": [{"id": "a1"}]}


def test_summary_only_keeps_the_acknowledgment_for_binary_payloads() -> None:
    """audio.synthesize returns base64 audio; text must stay a summary."""
    res = action_tools.result(
        {"audio_base64": "A" * 5000}, text="Synthesized 10 bytes.", summary_only=True
    )
    assert res.content[0].text == "Synthesized 10 bytes."
    assert res.structured_content is not None


@pytest.mark.parametrize("profile", ["connector", "chatgpt", "customer"])
async def test_every_advertised_tool_answers_in_the_text_block(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text-only host must see the payload for every tool on every profile.

    Stubs the API layer, invokes each advertised tool, and asserts the sentinel
    reaches the text block. Tools whose arguments cannot be synthesized here are
    exercised through their shared return path by the helper tests above.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)

    async def fake(*args: object, **kwargs: object) -> dict:
        return PAYLOAD

    for name in ("call_speko_api", "call_action", "call_speko_api_any"):
        monkeypatch.setattr(http_client, name, fake)
    monkeypatch.setattr(action_tools, "call", fake)

    mcp = create_server()
    checked = 0
    for tool in await mcp._list_tools():
        if tool.name in TEXT_EXEMPT_TOOLS or not isinstance(
            await mcp.get_tool(tool.name), ManifestActionTool
        ):
            continue
        res = await (await mcp.get_tool(tool.name)).run({})
        text = " ".join(c.text for c in res.content if hasattr(c, "text"))
        assert SENTINEL in text, (
            f"{tool.name} answered with {text[:80]!r} — a text-only host "
            "(claude.ai web) would show the user nothing."
        )
        checked += 1

    assert checked > 0, f"profile {profile} advertised no manifest tools to check"


def test_no_manifest_tool_returns_a_bare_acknowledgment() -> None:
    """Guards the exact string that shipped the outage."""
    source = (
        __import__("pathlib")
        .Path(__file__)
        .parent.parent.joinpath("src/spekoai_mcp/generated_action_tools.py")
        .read_text()
    )
    assert 'text=f"Executed {self.action_id}."' not in source
    assert len(action_entries()) > 0
