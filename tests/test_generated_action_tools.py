from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import fastmcp_request_ctx

from spekoai_mcp import http_client
from spekoai_mcp.action_manifest import action_entries
from spekoai_mcp.action_tools import DRIVE_MESSAGE
from spekoai_mcp.generated_action_tools import (
    ManifestActionTool,
    _idempotency_key,
    _open_output_schema,
)


def test_idempotency_key_is_never_shared_between_invocations() -> None:
    """JSON-RPC ids restart with every stateless connection, so two chats that
    each send request 3 with the same arguments must still get distinct keys;
    a shared key replays the first result (409 ONE_TIME_SECRET_NOT_REPLAYABLE
    on billing.portal.create in prod)."""
    arguments: dict[str, object] = {}
    first = fastmcp_request_ctx.set(SimpleNamespace(request_id=3))  # type: ignore[arg-type]
    try:
        key = _idempotency_key("billing.portal.create", arguments)
    finally:
        fastmcp_request_ctx.reset(first)
    second = fastmcp_request_ctx.set(SimpleNamespace(request_id=3))  # type: ignore[arg-type]
    try:
        assert _idempotency_key("billing.portal.create", arguments) != key
    finally:
        fastmcp_request_ctx.reset(second)
    assert key.startswith("mcp-")


def test_open_output_schema_drops_closed_objects_at_every_depth() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "data": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {"type": "array", "items": {"additionalProperties": False}}
                },
            },
            "extra": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    }
    opened = _open_output_schema(schema)
    assert "additionalProperties" not in opened
    assert "additionalProperties" not in opened["properties"]["data"]
    assert (
        "additionalProperties" not in opened["properties"]["data"]["properties"]["items"]["items"]
    )
    # A typed map is a real constraint, not a closed object; it stays.
    assert opened["properties"]["extra"]["additionalProperties"] == {"type": "string"}
    # The manifest entry itself is not mutated.
    assert schema["additionalProperties"] is False


def test_no_registered_output_schema_rejects_a_new_response_field() -> None:
    def closed(node: object) -> bool:
        if isinstance(node, dict):
            if node.get("additionalProperties") is False:
                return True
            return any(closed(value) for value in node.values())
        if isinstance(node, list):
            return any(closed(item) for item in node)
        return False

    for entry in action_entries():
        assert not closed(_open_output_schema(entry["outputSchema"])), entry["id"]


DRIVE_SHARE = "https://drive.google.com/file/d/abc123/view?usp=sharing"
CLONE_ARGUMENTS = {
    "name": "My voice",
    "audio_url": DRIVE_SHARE,
    "provider": "elevenlabs",
    "consent_confirmed": True,
}


def _clone_tool() -> ManifestActionTool:
    entry = next(e for e in action_entries() if e["id"] == "voice_clones.create")
    return ManifestActionTool(
        name=entry["id"],
        description=entry["description"],
        parameters=entry["inputSchema"],
        action_id=entry["id"],
        idempotency=entry["idempotency"],
    )


async def test_voice_clone_create_sends_the_download_link_of_a_share_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    async def call_action(_action_id: str, arguments: dict[str, Any], **_: Any) -> dict[str, Any]:
        sent.append(arguments)
        return {"kind": "result", "data": {"id": "vc_1"}}

    monkeypatch.setattr(http_client, "call_action", call_action)
    await _clone_tool().run(dict(CLONE_ARGUMENTS))

    assert sent[0]["audio_url"] == "https://drive.google.com/uc?export=download&id=abc123"
    assert sent[0]["name"] == "My voice"


@pytest.mark.parametrize(
    ("code", "message", "audio_url", "drive"),
    [
        (
            "VOICE_CLIP_UNSUPPORTED_TYPE",
            "audio_url returned a web page, not audio.",
            DRIVE_SHARE,
            True,
        ),
        ("VOICE_CLIP_FETCH_FAILED", "audio_url answered HTTP 403.", DRIVE_SHARE, True),
        ("VOICE_CLIP_FETCH_FAILED", "audio_url answered HTTP 401.", DRIVE_SHARE, True),
        ("VOICE_CLIP_FETCH_FAILED", "audio_url answered HTTP 404.", DRIVE_SHARE, False),
        ("VOICE_CLIP_FETCH_FAILED", "audio_url answered HTTP 429.", DRIVE_SHARE, False),
        ("VOICE_CLIP_FETCH_FAILED", "audio_url answered HTTP 500.", DRIVE_SHARE, False),
        (
            "VOICE_CLIP_FETCH_FAILED",
            "audio_url answered HTTP 403.",
            "https://example.com/me.mp3",
            False,
        ),
        (
            "VOICE_CLIP_UNSUPPORTED_TYPE",
            "audio_url returned a web page, not audio.",
            "https://example.com/me.mp3",
            False,
        ),
        ("VOICE_CLIP_FETCH_FAILED", "audio_url redirects too many times.", DRIVE_SHARE, False),
        ("VOICE_CLONE_PROVISION_FAILED", "audio too short", DRIVE_SHARE, False),
    ],
)
async def test_voice_clone_create_gives_drive_sharing_guidance_only_for_drive_access(
    monkeypatch: pytest.MonkeyPatch, code: str, message: str, audio_url: str, drive: bool
) -> None:
    async def call_action(*_: Any, **__: Any) -> dict[str, Any]:
        raise http_client.SpekoApiError(422, f"{code}: {message}", code=code)

    monkeypatch.setattr(http_client, "call_action", call_action)
    with pytest.raises((ToolError, http_client.SpekoApiError)) as raised:
        await _clone_tool().run({**CLONE_ARGUMENTS, "audio_url": audio_url})

    assert (str(raised.value) == DRIVE_MESSAGE) is drive
