"""The `audio` content block `audio.synthesize` adds for Claude Code hosts."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import AudioContent, TextContent

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
import spekoai_mcp.read_aloud_offer as read_aloud_offer
from spekoai_mcp import attribution
from spekoai_mcp.audio_block import _MAX_AUDIO_BLOCK_BYTES, with_audio_block
from spekoai_mcp.http_client import SpekoRawResponse
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.read_aloud_offer import READ_ALOUD_OFFER_ENV_VAR, READ_ALOUD_OFFER_TEXT

PCM = b"\x01\x00\x02\x00" * 6000
WAV = action_tools._pcm_to_wav(PCM, 24000)
SIGNED_URL = (
    "https://storage.googleapis.com/speko-action-media/org_1/abc.wav"
    "?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Signature=deadbeef"
)
BODY = {"text": "Your table is ready.", "intent": {"language": "en"}}

Identify = Callable[..., None]


@pytest.fixture
def identify() -> Iterator[Identify]:
    """Bind the client identity the way the ASGI edge and the call middleware do."""

    def bind(*, anthropic_client: str = "", client_info_name: str | None = None) -> None:
        http_client.set_current_anthropic_client(anthropic_client)
        http_client.set_current_client_info(
            {"name": client_info_name, "version": "2.1.322"} if client_info_name else None
        )

    yield bind
    # A token cannot be reset from the fixture's context; clear the values instead.
    http_client.set_current_anthropic_client("")
    http_client.set_current_client_info(None)


def _link_response(**overrides: Any) -> SpekoRawResponse:
    link = {
        "url": SIGNED_URL,
        "expiresAt": "2026-11-10T12:15:00.000Z",
        "contentType": "audio/wav",
        "sizeBytes": len(WAV),
        "sampleRate": 24000,
        "durationSeconds": 0.5,
        **overrides,
    }
    return SpekoRawResponse(
        content=json.dumps(link).encode(), content_type="application/json; charset=UTF-8"
    )


def _platform_returning(
    monkeypatch: pytest.MonkeyPatch, response: SpekoRawResponse, audio: bytes | None = WAV
) -> None:
    async def fake_raw(
        method: str, path: str, *, body: Any = None, timeout: float = 60.0
    ) -> SpekoRawResponse:
        return response

    async def fake_download(url: str) -> bytes | None:
        return audio

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "call_speko_api_raw", fake_raw)
    monkeypatch.setattr(action_tools, "_download_synthesized_audio", fake_download)


@pytest.mark.parametrize(
    ("anthropic_client", "client_info_name"),
    [
        ("Cowork", "claude-code"),
        ("Cowork", None),
        ("ClaudeCode", None),
        ("", "claude-code"),
    ],
)
async def test_claude_code_hosts_get_the_clip_as_an_audio_block(
    monkeypatch: pytest.MonkeyPatch,
    identify: Identify,
    anthropic_client: str,
    client_info_name: str | None,
) -> None:
    _platform_returning(monkeypatch, _link_response())
    identify(anthropic_client=anthropic_client, client_info_name=client_info_name)

    out = await action_tools.synthesize_speech(BODY)

    text, audio = out.content
    assert isinstance(text, TextContent)
    assert SIGNED_URL in text.text
    assert isinstance(audio, AudioContent)
    assert audio.mime_type == "audio/wav"
    assert audio.data == out.structured_content["audio_base64"]
    assert base64.b64decode(audio.data) == WAV
    # What goes over the wire: a spec `audio` block after the text block.
    wire_content, wire_structured = out.to_mcp_result()
    assert [block.type for block in wire_content] == ["text", "audio"]
    assert wire_structured == out.structured_content


@pytest.mark.parametrize(
    ("anthropic_client", "client_info_name"),
    [
        ("ClaudeAI", None),
        ("ClaudeAI", "Anthropic/ClaudeAI"),
        # claude.ai can send Claude Code's clientInfo.name; the header decides.
        ("ClaudeAI", "claude-code"),
        ("SomethingNew", "claude-code"),
        ("", "openai-mcp"),
        ("", "cursor-vscode"),
        ("", None),
    ],
)
async def test_other_clients_get_the_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    identify: Identify,
    anthropic_client: str,
    client_info_name: str | None,
) -> None:
    _platform_returning(monkeypatch, _link_response())
    baseline = await action_tools.synthesize_speech(BODY)
    identify(anthropic_client=anthropic_client, client_info_name=client_info_name)

    out = await action_tools.synthesize_speech(BODY)

    assert len(baseline.content) == 1
    assert out.content == baseline.content
    assert out.structured_content == baseline.structured_content
    assert out.to_mcp_result() == baseline.to_mcp_result()


async def test_a_clip_above_the_inline_cap_gets_no_block(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    _platform_returning(
        monkeypatch, _link_response(sizeBytes=action_tools._INLINE_AUDIO_MAX_BYTES + 1)
    )
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text"]
    assert "audio_base64" not in out.structured_content


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_clip_above_the_block_cap_keeps_the_result_it_had(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, streamed: bool
) -> None:
    """Two base64 copies of a larger clip pass the 16 MB a Claude Code client reads."""
    clip = b"\x00" * (_MAX_AUDIO_BLOCK_BYTES + 1)
    response = (
        SpekoRawResponse(content=clip, content_type="audio/wav")
        if streamed
        else _link_response(sizeBytes=len(clip))
    )
    _platform_returning(monkeypatch, response, audio=clip)
    baseline = await action_tools.synthesize_speech(BODY)
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text"]
    assert out.to_mcp_result() == baseline.to_mcp_result()
    assert base64.b64decode(out.structured_content["audio_base64"]) == clip


async def test_a_clip_at_the_block_cap_gets_the_block_in_a_readable_response(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    clip = b"\x00" * _MAX_AUDIO_BLOCK_BYTES
    _platform_returning(monkeypatch, SpekoRawResponse(content=clip, content_type="audio/mpeg"))
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text", "audio"]
    assert out.content[1].mime_type == "audio/mpeg"
    wire = len(out.content[1].data) + len(out.structured_content["audio_base64"])
    assert wire < 15_000_000


async def test_the_router_path_gets_the_block(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    async def fake_router(payload: dict[str, Any], *, token: str) -> Any:
        return SimpleNamespace(content=PCM, provider="cartesia", model="sonic-3")

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_test")
    monkeypatch.setattr(http_client, "post_router_speech", fake_router)
    identify(client_info_name="claude-code")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text", "audio"]
    assert out.content[1].mime_type == "audio/wav"
    assert base64.b64decode(out.content[1].data)[:4] == b"RIFF"


@pytest.mark.parametrize("content_type", ["audio/pcm;rate=24000", "audio/L16", "text/html"])
async def test_a_clip_that_is_not_a_playable_audio_file_gets_no_block(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, content_type: str
) -> None:
    _platform_returning(monkeypatch, SpekoRawResponse(content=PCM, content_type=content_type))
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text"]


async def test_a_content_type_parameter_is_dropped_from_the_mime_type(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    _platform_returning(
        monkeypatch, SpekoRawResponse(content=WAV, content_type="Audio/WAV; codecs=1")
    )
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert out.content[1].mime_type == "audio/wav"


async def test_a_failed_read_back_leaves_the_link_and_no_block(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    _platform_returning(monkeypatch, _link_response(), audio=None)
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text"]
    assert SIGNED_URL in out.content[0].text


async def test_the_read_aloud_offer_still_lands_on_the_text_block(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    """Cowork sends claude.ai's User-Agent, so it is in the offer experiment too."""
    token = SimpleNamespace(
        claims={"organization_id": "org_1"}, client_id="oauth-client", subject="user_1"
    )
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    monkeypatch.setenv(READ_ALOUD_OFFER_ENV_VAR, "true")
    monkeypatch.setattr(read_aloud_offer, "get_access_token", lambda: token)
    monkeypatch.setattr(profiles, "get_access_token", lambda: token)
    monkeypatch.setattr(read_aloud_offer, "current_client_ua", lambda: "Claude-User")
    monkeypatch.setattr(attribution, "submit_event", lambda event: None)
    _platform_returning(monkeypatch, _link_response())
    identify(anthropic_client="Cowork", client_info_name="claude-code")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text", "audio"]
    assert out.content[0].text.endswith(f"\n{READ_ALOUD_OFFER_TEXT}")
    assert base64.b64decode(out.content[1].data) == WAV


def test_a_failure_building_the_block_returns_the_original_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import spekoai_mcp.audio_block as audio_block

    def boom() -> bool:
        raise RuntimeError("identity lookup failed")

    monkeypatch.setattr(audio_block, "client_takes_audio_block", boom)
    original = action_tools.result(
        {"audio_base64": "AAAA", "content_type": "audio/wav"}, text="ok", summary_only=True
    )

    assert with_audio_block(original) is original
