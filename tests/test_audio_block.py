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
from spekoai_mcp.audio_block import (
    _MAX_TWO_COPY_BYTES,
    AUDIO_BLOCK_TOO_LARGE_TEXT,
    MAX_AUDIO_BLOCK_BYTES,
    with_audio_block,
)
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


async def test_a_clip_above_the_inline_cap_gets_no_block_and_a_line_that_says_so(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    _platform_returning(
        monkeypatch, _link_response(sizeBytes=action_tools._INLINE_AUDIO_MAX_BYTES + 1)
    )
    baseline = await action_tools.synthesize_speech(BODY)
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text"]
    assert out.content[0].text == f"{baseline.content[0].text}\n{AUDIO_BLOCK_TOO_LARGE_TEXT}"
    assert out.structured_content == baseline.structured_content
    assert "audio_base64" not in out.structured_content
    assert AUDIO_BLOCK_TOO_LARGE_TEXT not in baseline.content[0].text


async def test_a_streamed_clip_above_the_inline_cap_keeps_the_result_it_had(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    """The streamed path has no cap and no link; nothing is added or removed."""
    clip = b"\x00" * (MAX_AUDIO_BLOCK_BYTES + 1)
    _platform_returning(monkeypatch, SpekoRawResponse(content=clip, content_type="audio/wav"))
    baseline = await action_tools.synthesize_speech(BODY)
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert out.to_mcp_result() == baseline.to_mcp_result()


@pytest.mark.parametrize("size", [_MAX_TWO_COPY_BYTES + 1, MAX_AUDIO_BLOCK_BYTES])
async def test_a_linked_clip_too_large_to_send_twice_gets_the_block_alone(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, size: int
) -> None:
    """Two base64 copies of this clip pass the 16 MB a Claude Code client reads."""
    clip = b"\x01" * size
    _platform_returning(monkeypatch, _link_response(sizeBytes=size), audio=clip)
    baseline = await action_tools.synthesize_speech(BODY)
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text", "audio"]
    assert base64.b64decode(out.content[1].data) == clip
    assert out.content[0].text == baseline.content[0].text
    # Everything but the second copy of the clip is still there, the link
    # included: it is what the bundled player offers without `audio_base64`.
    assert "audio_base64" not in out.structured_content
    assert out.structured_content == {
        key: value for key, value in baseline.structured_content.items() if key != "audio_base64"
    }
    assert out.structured_content["url"] == SIGNED_URL
    assert out.structured_content["size_bytes"] == size
    assert len(out.content[1].data) < 12_000_000
    # Other clients still get the base64 for the same clip.
    assert base64.b64decode(baseline.structured_content["audio_base64"]) == clip


@pytest.mark.parametrize("size", [_MAX_TWO_COPY_BYTES + 1, MAX_AUDIO_BLOCK_BYTES])
async def test_a_clip_with_no_link_too_large_to_send_twice_keeps_the_result_it_had(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, size: int
) -> None:
    """The player has only `audio_base64` to play here, so it is not removed."""
    clip = b"\x01" * size
    _platform_returning(monkeypatch, SpekoRawResponse(content=clip, content_type="audio/wav"))
    baseline = await action_tools.synthesize_speech(BODY)
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert "url" not in out.structured_content
    assert out.to_mcp_result() == baseline.to_mcp_result()
    assert base64.b64decode(out.structured_content["audio_base64"]) == clip


async def test_a_router_clip_too_large_to_send_twice_keeps_the_result_it_had(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    pcm = b"\x01\x00" * (_MAX_TWO_COPY_BYTES // 2 + 1000)

    async def fake_router(payload: dict[str, Any], *, token: str) -> Any:
        return SimpleNamespace(content=pcm, provider="cartesia", model="sonic-3")

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_test")
    monkeypatch.setattr(http_client, "post_router_speech", fake_router)
    baseline = await action_tools.synthesize_speech(BODY)
    identify(client_info_name="claude-code")

    out = await action_tools.synthesize_speech(BODY)

    assert out.to_mcp_result() == baseline.to_mcp_result()
    assert "audio_base64" in out.structured_content


async def test_a_clip_at_the_two_copy_cap_keeps_audio_base64(
    monkeypatch: pytest.MonkeyPatch, identify: Identify
) -> None:
    clip = b"\x00" * _MAX_TWO_COPY_BYTES
    _platform_returning(monkeypatch, SpekoRawResponse(content=clip, content_type="audio/mpeg"))
    identify(anthropic_client="Cowork")

    out = await action_tools.synthesize_speech(BODY)

    assert [block.type for block in out.content] == ["text", "audio"]
    assert out.content[1].mime_type == "audio/mpeg"
    assert out.structured_content["audio_base64"] == out.content[1].data
    wire = len(out.content[1].data) + len(out.structured_content["audio_base64"])
    assert wire < 15_000_000


def test_the_block_cap_is_the_inline_cap() -> None:
    assert MAX_AUDIO_BLOCK_BYTES == action_tools._INLINE_AUDIO_MAX_BYTES


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


# --- audio.synthesis.get ---------------------------------------------------

SYNTHESIS_ID = "0b6f3c1e-9a52-4c1d-8e2f-3a4b5c6d7e8f"


def _stored_link(
    monkeypatch: pytest.MonkeyPatch, audio: bytes | None = WAV, **overrides: Any
) -> list[str]:
    """Stub the link endpoint and the clip read; return the URLs that were read."""
    fetched: list[str] = []

    async def fake_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
        return {
            "id": SYNTHESIS_ID,
            "url": SIGNED_URL,
            "expiresAt": "2026-11-10T12:15:00.000Z",
            "contentType": "audio/wav",
            "sizeBytes": len(WAV),
            **overrides,
        }

    async def fake_download(url: str) -> bytes | None:
        fetched.append(url)
        return audio

    monkeypatch.setattr(http_client, "call_speko_api", fake_api)
    monkeypatch.setattr(action_tools, "_download_synthesized_audio", fake_download)
    return fetched


@pytest.mark.parametrize(
    ("anthropic_client", "client_info_name"),
    [("Cowork", "claude-code"), ("ClaudeCode", None), ("", "claude-code")],
)
async def test_synthesis_get_gives_claude_code_hosts_the_stored_clip(
    monkeypatch: pytest.MonkeyPatch,
    identify: Identify,
    anthropic_client: str,
    client_info_name: str | None,
) -> None:
    fetched = _stored_link(monkeypatch)
    baseline = await action_tools.get_synthesis_link(SYNTHESIS_ID)
    assert fetched == []
    identify(anthropic_client=anthropic_client, client_info_name=client_info_name)

    out = await action_tools.get_synthesis_link(SYNTHESIS_ID)

    assert fetched == [SIGNED_URL]
    assert [block.type for block in out.content] == ["text", "audio"]
    assert out.content[1].mime_type == "audio/wav"
    assert base64.b64decode(out.content[1].data) == WAV
    # The link result itself is the one every client gets.
    assert out.content[0] == baseline.content[0]
    assert out.structured_content == baseline.structured_content
    assert "audio_base64" not in out.structured_content


@pytest.mark.parametrize(
    ("anthropic_client", "client_info_name"),
    [("ClaudeAI", None), ("ClaudeAI", "claude-code"), ("", "openai-mcp"), ("", None)],
)
async def test_synthesis_get_is_unchanged_and_reads_nothing_for_other_clients(
    monkeypatch: pytest.MonkeyPatch,
    identify: Identify,
    anthropic_client: str,
    client_info_name: str | None,
) -> None:
    fetched = _stored_link(monkeypatch)
    baseline = await action_tools.get_synthesis_link(SYNTHESIS_ID)
    identify(anthropic_client=anthropic_client, client_info_name=client_info_name)

    out = await action_tools.get_synthesis_link(SYNTHESIS_ID)

    assert fetched == []
    assert out.to_mcp_result() == baseline.to_mcp_result()


@pytest.mark.parametrize(
    "overrides",
    [
        {"sizeBytes": MAX_AUDIO_BLOCK_BYTES + 1},
        {"sizeBytes": None},
        {"sizeBytes": 0},
        {"contentType": None},
        {"contentType": "audio/pcm;rate=24000"},
        {"contentType": "application/octet-stream"},
        {"url": "http://storage.googleapis.com/speko-action-media/org_1/abc.wav"},
    ],
)
async def test_synthesis_get_keeps_the_link_alone_when_the_clip_cannot_be_attached(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, overrides: dict[str, Any]
) -> None:
    fetched = _stored_link(monkeypatch, **overrides)
    baseline = await action_tools.get_synthesis_link(SYNTHESIS_ID)
    identify(anthropic_client="Cowork")

    out = await action_tools.get_synthesis_link(SYNTHESIS_ID)

    assert fetched == []
    assert out.to_mcp_result() == baseline.to_mcp_result()


@pytest.mark.parametrize("audio", [None, b"", b"\x00" * (MAX_AUDIO_BLOCK_BYTES + 1)])
async def test_synthesis_get_keeps_the_link_alone_when_the_read_fails_or_is_too_large(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, audio: bytes | None
) -> None:
    fetched = _stored_link(monkeypatch, audio=audio)
    baseline = await action_tools.get_synthesis_link(SYNTHESIS_ID)
    identify(anthropic_client="Cowork")

    out = await action_tools.get_synthesis_link(SYNTHESIS_ID)

    assert fetched == [SIGNED_URL]
    assert out.to_mcp_result() == baseline.to_mcp_result()


async def test_attaching_a_block_is_logged(
    monkeypatch: pytest.MonkeyPatch, identify: Identify, caplog: pytest.LogCaptureFixture
) -> None:
    _platform_returning(monkeypatch, _link_response())
    _ = _stored_link(monkeypatch)
    identify(anthropic_client="Cowork", client_info_name="claude-code")

    with caplog.at_level("INFO", logger="spekoai_mcp.audio_block"):
        await action_tools.synthesize_speech(BODY)
        await action_tools.get_synthesis_link(SYNTHESIS_ID)

    records = [r for r in caplog.records if getattr(r, "event", None) == "audio_block_attached"]
    assert [(r.tool, r.audio_bytes) for r in records] == [
        ("audio.synthesize", len(WAV)),
        ("audio.synthesis.get", len(WAV)),
    ]
    assert SIGNED_URL not in caplog.text
