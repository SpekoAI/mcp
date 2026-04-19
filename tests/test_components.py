"""Tests for the `spekoai://components/react/voice-session` resource."""

from __future__ import annotations

from spekoai_mcp.server import create_server


async def test_component_resource_advertised() -> None:
    mcp = create_server()
    resources = await mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "spekoai://components/react/voice-session" in uris


async def test_component_resource_body_shape() -> None:
    mcp = create_server()
    result = await mcp.read_resource(
        "spekoai://components/react/voice-session"
    )
    body = result.contents[0].content
    assert isinstance(body, str)
    assert body.startswith("'use client';")
    assert "export function SpekoVoiceSession" in body
    assert "@livekit/components-react" in body
    assert "AgentSessionProvider" in body


async def test_component_resource_mime_is_plain_text() -> None:
    mcp = create_server()
    resources = await mcp.list_resources()
    match = next(
        r
        for r in resources
        if str(r.uri) == "spekoai://components/react/voice-session"
    )
    assert match.mime_type == "text/plain"
