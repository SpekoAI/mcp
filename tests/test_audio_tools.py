"""Tests for the speech synthesis and transcription tools.

Two things are being pinned here:

1. The tools work — correct endpoint, correct body, and a transcript
   assembled out of the SSE frames `/v1/transcribe` actually answers with.
2. `audio.synthesize` is absent from the Anthropic host's surface while
   `audio.transcribe` stays. Anthropic's Software Directory Policy prohibits
   software that generates audio content, so the published listing omits
   synthesis; transcription returns text and is unaffected. Direct MCP
   clients keep both on the default `/mcp` path.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import socket
import traceback
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import NotFoundError, ToolError

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.http_client as http_client
from spekoai_mcp.http_client import SpekoRawResponse
from spekoai_mcp.profiles import (
    CONNECTOR_EXCLUDED_TOOL_NAMES,
    CONNECTOR_PROFILE,
    DEFAULT_PROFILE_ENV_VAR,
)
from spekoai_mcp.server import create_server

MP3 = b"ID3\x04\x00audio-bytes"
DRIVE_MESSAGE = (
    "Google requires access to this file, so Speko received Google's web page instead of "
    "the audio. In Google Drive open Share, set General access to 'Anyone with the link', "
    "then send the same link again."
)
NO_SPEECH_MESSAGE = (
    "No speech was recognized in the audio. Check the recording before trying again."
)
INCOMPLETE_MESSAGE = "Speko returned an incomplete transcription response. Try again."


def _web_page_message(host: str, kind: str) -> str:
    return (
        f"The link returned a web page ({kind}) from {host}, not an audio file. "
        "Send a link that downloads the audio directly "
        "(mp3, wav, m4a, ogg, flac, webm) without a sign-in."
    )


def _public_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]


class _FakeNetworkStream:
    """Stands in for the live socket httpx exposes via extensions."""

    def __init__(self, peer: str) -> None:
        self._peer = peer

    def get_extra_info(self, name: str) -> Any:
        return (self._peer, 443) if name == "server_addr" else None


class _FakeStream:
    """One httpx streaming response."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        peer: str | None = "93.184.216.34",
    ) -> None:
        self.status_code = status_code
        # The fetch must use its redirect walk, not a response URL supplied by a transport.
        self.url = httpx.URL("https://unrelated-response.example.com/")
        self.headers = headers or {}
        self._chunks = chunks or []
        self.extensions: dict[str, Any] = {}
        if peer is not None:
            self.extensions["network_stream"] = _FakeNetworkStream(peer)

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        httpx.Response(self.status_code, request=httpx.Request("GET", self.url)).raise_for_status()

    async def aiter_bytes(self) -> Any:
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _client_returning(
    response: _FakeStream | list[_FakeStream],
    *,
    requested: list[str] | None = None,
    client_options: list[dict[str, Any]] | None = None,
) -> Any:
    """Allow a real redirect walk while keeping every response deterministic."""

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self._responses = iter(response) if isinstance(response, list) else None
            if client_options is not None:
                client_options.append(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _FakeStream:
            if requested is not None:
                requested.append(url)
            if self._responses is not None:
                return next(self._responses)
            assert isinstance(response, _FakeStream)
            return response

    return FakeClient


def _force_deployment_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    """Configure the tool surface selected by one deployed host."""
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)


# --- the tools are registered ----------------------------------------------


async def test_both_audio_tools_are_on_the_default_surface() -> None:
    names = [tool.name for tool in await create_server().list_tools()]
    assert "audio.synthesize" in names
    assert "audio.transcribe" in names


# --- synthesis -------------------------------------------------------------


async def test_synthesize_posts_the_body_and_returns_base64_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_raw(method: str, path: str, *, body: Any = None) -> SpekoRawResponse:
        seen.update(method=method, path=path, body=body)
        return SpekoRawResponse(content=MP3, content_type="audio/mpeg")

    monkeypatch.setattr(http_client, "call_speko_api_raw", fake_raw)

    body = {"text": "Your table is ready.", "intent": {"language": "en"}, "sampleRate": 24000}
    out = await action_tools.synthesize_speech(body)

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/synthesize"
    assert seen["body"] is body
    assert out.structured_content == {
        "audio_base64": base64.b64encode(MP3).decode("ascii"),
        "content_type": "audio/mpeg",
        "size_bytes": len(MP3),
        "sample_rate": 24000,
    }
    assert base64.b64decode(out.structured_content["audio_base64"]) == MP3


async def test_synthesize_rejects_an_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silent success is worse than an error: empty audio must not look synthesized."""

    async def fake_raw(method: str, path: str, *, body: Any = None) -> SpekoRawResponse:
        return SpekoRawResponse(content=b"", content_type="audio/mpeg")

    monkeypatch.setattr(http_client, "call_speko_api_raw", fake_raw)

    with pytest.raises(ToolError, match="empty body"):
        await action_tools.synthesize_speech({"text": "hi", "intent": {"language": "en"}})


PCM = b"\x00\x01" * 64


async def test_synthesize_prefers_the_router_with_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_router(
        request_payload: dict[str, Any], *, token: str
    ) -> http_client.RouterAudioResponse:
        seen.update(payload=request_payload, token=token)
        return http_client.RouterAudioResponse(
            content=PCM,
            content_type="application/octet-stream",
            provider="inworld",
            model="inworld-tts-2",
        )

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the Platform endpoint must not be reached")

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_speech", fake_router)
    monkeypatch.setattr(http_client, "call_speko_api_raw", unreachable)

    out = await action_tools.synthesize_speech(
        {
            "text": "Your table is ready.",
            "intent": {"language": "es-MX", "optimizeFor": "accuracy"},
            "voice": "Aoede",
            "sampleRate": 24000,
        }
    )

    assert seen["token"] == "sk_live_test"
    assert seen["payload"] == {
        # Platform's `accuracy` is the Router's `quality`.
        "routing": {"mode": "auto", "objective": "quality"},
        "input": "Your table is ready.",
        "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 24000, "channels": 1},
        "voice": "Aoede",
        "language": "es-MX",
    }
    # The Router labels the stream application/octet-stream; the tool reports
    # the format the request asked for and got, so a client can play it.
    assert out.structured_content["content_type"] == "audio/pcm;rate=24000"
    assert out.structured_content["provider"] == "inworld"
    assert out.structured_content["model"] == "inworld-tts-2"
    assert base64.b64decode(out.structured_content["audio_base64"]) == PCM


async def test_synthesize_maps_a_single_pin_to_explicit_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_router(
        request_payload: dict[str, Any], *, token: str
    ) -> http_client.RouterAudioResponse:
        seen["payload"] = request_payload
        return http_client.RouterAudioResponse(PCM, "application/octet-stream", None, None)

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_speech", fake_router)

    await action_tools.synthesize_speech(
        {
            "text": "hi",
            "intent": {"language": "en"},
            "constraints": {"allowedProviders": {"tts": ["cartesia:sonic-3.5"]}},
        }
    )

    assert seen["payload"]["routing"] == {
        "mode": "explicit",
        "provider": "cartesia",
        "model": "sonic-3.5",
    }
    # Unstated sample rate still has to reach the Router: its body requires one.
    assert seen["payload"]["audio"]["sample_rate_hz"] == 24000


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"text": "hi", "intent": {"language": "en"}, "speed": 1.2}, "speed"),
        ({"text": "hi", "intent": {"language": "en"}, "spokenForm": True}, "spokenForm"),
        ({"text": "hi", "intent": {"language": "en"}, "instructions": "warmly"}, "instructions"),
        ({"text": "hi", "intent": {"language": "en"}, "model": "sonic-2"}, "bare model id"),
        ({"text": "hi", "intent": {"language": "en", "region": "us"}}, "intent.region"),
        (
            {
                "text": "hi",
                "intent": {"language": "en"},
                "constraints": {"allowedProviders": {"tts": ["a:1", "b:2"]}},
            },
            "a candidate set",
        ),
    ],
)
async def test_synthesize_stays_on_platform_for_what_the_router_cannot_say(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], why: str
) -> None:
    """Losing a knob silently is worse than using the older endpoint."""
    seen: dict[str, Any] = {}

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{why} has no Router equivalent and must not be dropped")

    async def fake_raw(method: str, path: str, *, body: Any = None) -> SpekoRawResponse:
        seen["path"] = path
        return SpekoRawResponse(content=MP3, content_type="audio/mpeg")

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_speech", unreachable)
    monkeypatch.setattr(http_client, "call_speko_api_raw", fake_raw)

    await action_tools.synthesize_speech(body)

    assert seen["path"] == "/v1/synthesize"


async def test_synthesize_falls_back_when_the_router_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def failing_router(*args: Any, **kwargs: Any) -> Any:
        raise http_client.SpekoApiError(400, "refused", code="capability_unsupported")

    async def fake_raw(method: str, path: str, *, body: Any = None) -> SpekoRawResponse:
        seen["path"] = path
        return SpekoRawResponse(content=MP3, content_type="audio/mpeg")

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_speech", failing_router)
    monkeypatch.setattr(http_client, "call_speko_api_raw", fake_raw)

    out = await action_tools.synthesize_speech({"text": "hi", "intent": {"language": "en"}})

    assert seen["path"] == "/v1/synthesize"
    assert out.structured_content["content_type"] == "audio/mpeg"


async def test_synthesize_raises_a_router_provider_error_rather_than_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying a provider failure on Platform synthesizes and bills twice."""

    async def failing_router(*args: Any, **kwargs: Any) -> Any:
        raise http_client.SpekoApiError(502, "upstream died", code="provider_error")

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a provider error must not fall back")

    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_speech", failing_router)
    monkeypatch.setattr(http_client, "call_speko_api_raw", unreachable)

    with pytest.raises(http_client.SpekoApiError):
        await action_tools.synthesize_speech({"text": "hi", "intent": {"language": "en"}})


# --- transcription ---------------------------------------------------------


async def test_transcribe_forwards_bytes_with_the_intent_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        seen["url"] = url
        return MP3, "audio/mpeg"

    async def fake_post(
        path: str,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> SpekoRawResponse:
        seen.update(path=path, payload=payload, content_type=content_type, headers=extra_headers)
        return SpekoRawResponse(
            content=(
                b'event: meta\ndata: {"provider": "assemblyai", "model": "universal"}\n\n'
                b'event: transcript\ndata: {"text": "Your table", "isFinal": true}\n\n'
                b'event: done\ndata: {"text": "Your table is ready.", "confidence": 0.97}\n\n'
            ),
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    out = await action_tools.transcribe_audio(
        "https://storage.example.com/rec.mp3", language="es-MX", keywords=["Speko"]
    )

    assert seen["url"] == "https://storage.example.com/rec.mp3"
    assert seen["path"] == "/v1/transcribe"
    assert seen["payload"] == MP3
    assert seen["content_type"] == "audio/mpeg"
    assert json.loads(seen["headers"]["X-Speko-Intent"]) == {"language": "es-MX"}
    assert json.loads(seen["headers"]["X-Speko-Stt-Options"]) == {"keywords": ["Speko"]}
    # `done.text` only -- not the incremental final appended to it as well.
    assert out.structured_content == {"text": "Your table is ready.", "language": "es-MX"}


async def test_transcribe_rejects_a_missing_done_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incremental text cannot prove that the backend completed the recording."""

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return MP3, "audio/mpeg"

    async def fake_post(*args: Any, **kwargs: Any) -> SpekoRawResponse:
        return SpekoRawResponse(
            content=b'event: transcript\ndata: {"text": "partial", "isFinal": true}\n\n',
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    with pytest.raises(ToolError) as error:
        await action_tools.transcribe_audio("https://cdn.example.com/recording.mp3")

    assert str(error.value) == INCOMPLETE_MESSAGE


# --- SSE semantics ---------------------------------------------------------


def test_done_text_wins_over_the_incremental_finals() -> None:
    """The route sends finals AND an assembled `done.text`; using both duplicates it."""
    stream = (
        'event: transcript\ndata: {"text": "Your table", "isFinal": true}\n\n'
        'event: transcript\ndata: {"text": "is ready.", "isFinal": true}\n\n'
        'event: done\ndata: {"text": "Your table is ready."}\n\n'
    )
    assert action_tools._transcript_from_sse(stream) == "Your table is ready."


def test_finals_are_the_fallback_when_no_done_frame_arrives() -> None:
    stream = (
        'event: transcript\ndata: {"text": "Your table", "isFinal": true}\n\n'
        'event: transcript\ndata: {"text": "is rea", "isFinal": false}\n\n'
        'event: transcript\ndata: {"text": "is ready.", "isFinal": true}\n\n'
    )
    assert action_tools._transcript_from_sse(stream) == "Your table is ready."


def test_an_error_frame_raises_even_though_the_status_was_200() -> None:
    """An SSE `error` frame arrives with HTTP 200; swallowing it reads as silence."""
    stream = (
        'event: meta\ndata: {"provider": "assemblyai"}\n\n'
        'event: error\ndata: {"error": "provider_unavailable", "code": "upstream"}\n\n'
    )
    with pytest.raises(ToolError, match="Transcription failed"):
        action_tools._transcript_from_sse(stream)


def test_meta_frame_contributes_no_text() -> None:
    stream = 'event: meta\ndata: {"provider": "deepgram", "model": "nova-3"}\n\n'
    assert action_tools._transcript_from_sse(stream) == ""


def test_malformed_and_empty_frames_are_skipped() -> None:
    stream = (
        "event: transcript\ndata: not-json\n\n"
        "event: transcript\n\n"
        'event: transcript\ndata: {"text": "hello", "isFinal": true}\n\n'
        "data: [DONE]\n\n"
    )
    assert action_tools._transcript_from_sse(stream) == "hello"


def test_empty_stream_yields_empty_transcript() -> None:
    assert action_tools._transcript_from_sse("") == ""


# --- fetching the audio is not an SSRF primitive ---------------------------


WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32


def _router_response(words: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": "Your table is ready.",
        "segments": [{"text": "Your table is ready.", "start_ms": 0, "end_ms": 1400}],
        "route": {
            "provider": "gemini",
            "model": "gemini-3.5-transcribe",
            "region": "us-east-1",
            "attempt_id": "ratt_1",
        },
        "usage": {"duration_ms": 1400},
    }
    if words:
        payload["words"] = [
            {"text": "Your", "start_ms": 0, "end_ms": 300},
            {"text": "table", "start_ms": 320, "end_ms": 700},
        ]
    return payload


async def test_transcribe_prefers_the_router_for_wav_with_an_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return WAV, "audio/wav"

    async def fake_router(
        audio: bytes,
        *,
        request_payload: dict[str, Any],
        token: str,
        content_type: str = "audio/wav",
    ) -> dict[str, Any]:
        seen.update(audio=audio, request=request_payload, token=token)
        return _router_response()

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the Platform endpoint must not be reached")

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_transcription", fake_router)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", unreachable)

    out = await action_tools.transcribe_audio(
        "https://storage.example.com/rec.wav",
        language="uz",
        keywords=["Speko"],
        word_timestamps=True,
    )

    assert seen["audio"] == WAV
    assert seen["token"] == "sk_live_test"
    # Automatic routing, not a pin: the Router filters candidates on the
    # capability itself and lands on a model that can answer.
    assert seen["request"]["routing"] == {"mode": "auto", "objective": "balanced"}
    assert seen["request"]["language"] == "uz"
    assert seen["request"]["options"] == {"keywords": ["Speko"], "word_timestamps": True}
    assert out.structured_content == {
        "text": "Your table is ready.",
        "language": "uz",
        "provider": "gemini",
        "model": "gemini-3.5-transcribe",
        "words": [
            {"text": "Your", "start_ms": 0, "end_ms": 300},
            {"text": "table", "start_ms": 320, "end_ms": 700},
        ],
    }


async def test_transcribe_stays_on_platform_for_a_container_the_router_cannot_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call recording is Ogg/Opus and the Router answers 415 for it."""
    seen: dict[str, Any] = {}

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return MP3, "audio/mpeg"

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a non-WAV upload must never reach the Router")

    async def fake_post(
        path: str,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> SpekoRawResponse:
        seen.update(path=path, content_type=content_type)
        return SpekoRawResponse(
            content=b'event: done\ndata: {"text": "Your table is ready."}\n\n',
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_transcription", unreachable)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    out = await action_tools.transcribe_audio("https://storage.example.com/rec.mp3")

    assert seen["path"] == "/v1/transcribe"
    assert seen["content_type"] == "audio/mpeg"
    assert out.structured_content["text"] == "Your table is ready."


async def test_transcribe_stays_on_platform_without_a_speko_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OAuth-delegated session holds no credential the Router accepts."""
    called: dict[str, Any] = {}

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return WAV, "audio/wav"

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a session without an API key must not reach the Router")

    async def fake_post(
        path: str,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> SpekoRawResponse:
        called["path"] = path
        return SpekoRawResponse(
            content=b'event: done\ndata: {"text": "ok"}\n\n',
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_router_transcription", unreachable)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    await action_tools.transcribe_audio("https://storage.example.com/rec.wav")

    assert called["path"] == "/v1/transcribe"


@pytest.mark.parametrize(
    ("code", "falls_back"),
    [
        ("unsupported_media", True),
        ("capability_unsupported", True),
        ("authentication_failed", True),
        ("provider_error", False),
        ("request_timeout", False),
    ],
)
async def test_router_refusals_fall_back_only_when_platform_can_serve_them(
    monkeypatch: pytest.MonkeyPatch, code: str, falls_back: bool
) -> None:
    """A provider failure is a real failure — retrying it bills the audio twice."""

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return WAV, "audio/wav"

    async def failing_router(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise http_client.SpekoApiError(400, f"{code}: refused", code=code)

    async def fake_post(
        path: str,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> SpekoRawResponse:
        return SpekoRawResponse(
            content=b'event: done\ndata: {"text": "fallback"}\n\n',
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_transcription", failing_router)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    if falls_back:
        out = await action_tools.transcribe_audio("https://storage.example.com/rec.wav")
        assert out.structured_content["text"] == "fallback"
    else:
        with pytest.raises(http_client.SpekoApiError):
            await action_tools.transcribe_audio("https://storage.example.com/rec.wav")


async def test_platform_word_timestamps_pin_the_capable_model_and_arrive_in_ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpinned, Platform answers 422: it demotes rather than selects."""
    seen: dict[str, Any] = {}

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return MP3, "audio/mpeg"

    async def fake_post(
        path: str,
        payload: bytes,
        *,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> SpekoRawResponse:
        seen["headers"] = extra_headers
        return SpekoRawResponse(
            content=(
                b'event: done\ndata: {"text": "Salom dunyo", "provider": "gemini", '
                b'"model": "gemini-3.5-transcribe", "words": ['
                b'{"text": "Salom", "start": 0.12, "end": 0.48}, '
                b'{"text": "dunyo", "start": 0.51, "end": 1}]}\n\n'
            ),
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    out = await action_tools.transcribe_audio(
        "https://storage.example.com/rec.mp3", language="uz", word_timestamps=True
    )

    assert json.loads(seen["headers"]["X-Speko-Stt-Options"]) == {"wordTimestamps": True}
    assert json.loads(seen["headers"]["X-Speko-Constraints"]) == {
        "allowedProviders": {"stt": ["gemini:gemini-3.5-transcribe"]}
    }
    assert out.structured_content["words"] == [
        {"text": "Salom", "start_ms": 120, "end_ms": 480},
        {"text": "dunyo", "start_ms": 510, "end_ms": 1000},
    ]
    assert out.structured_content["provider"] == "gemini"


async def test_transcribe_refuses_non_https_urls() -> None:
    for url in ("http://example.com/a.mp3", "file:///etc/passwd", "s3://bucket/a.mp3"):
        with pytest.raises(ToolError, match="https"):
            await action_tools.transcribe_audio(url)


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # cloud metadata -- the service account token
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "::1",  # loopback, v6
    ],
)
async def test_urls_resolving_to_non_public_addresses_are_refused(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """https alone proves nothing -- a public name can resolve anywhere."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

    monkeypatch.setattr(action_tools.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ToolError, match="non-public address"):
        await action_tools.transcribe_audio("https://evil.example.com/a.mp3")


async def test_a_redirect_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dangerous hop is the second one, so every hop is validated."""
    resolutions = iter(
        [
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))],
            [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("169.254.169.254", 443),
                )
            ],
        ]
    )
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", lambda *a, **k: next(resolutions))
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(
                status_code=302,
                headers={"location": "https://metadata.example.com/token"},
            )
        ),
    )

    with pytest.raises(ToolError, match="non-public address"):
        await action_tools._fetch_audio("https://cdn.example.com/rec.mp3")


async def test_an_oversized_body_is_refused_before_it_is_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming with a cap: a huge URL must not OOM the process serving every tool."""
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    chunk = b"\x00" * (1024 * 1024)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(_FakeStream(chunks=[chunk] * 30)),
    )

    with pytest.raises(ToolError, match="exceeds the 25 MB limit"):
        await action_tools._fetch_audio("https://cdn.example.com/big.wav")


async def test_an_empty_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx, "AsyncClient", _client_returning(_FakeStream(chunks=[]))
    )

    with pytest.raises(ToolError, match="is empty"):
        await action_tools._fetch_audio("https://cdn.example.com/silence.wav")


async def test_a_public_url_is_fetched_with_its_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(chunks=[MP3[:4], MP3[4:]], headers={"content-type": "audio/mpeg"})
        ),
    )

    assert await action_tools._fetch_audio("https://cdn.example.com/rec.mp3") == (MP3, "audio/mpeg")


# --- the connector surface omits synthesis ---------------------------------


async def test_connector_profile_hides_synthesis_but_keeps_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, CONNECTOR_PROFILE)
    names = [tool.name for tool in await create_server().list_tools()]
    assert "audio.synthesize" not in names
    assert "audio.transcribe" in names


async def test_connector_profile_refuses_to_call_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden must also mean uncallable, or the filter is cosmetic."""
    _force_deployment_profile(monkeypatch, CONNECTOR_PROFILE)
    with pytest.raises(NotFoundError, match="Unknown tool"):
        await create_server().call_tool(
            "audio.synthesize", {"body": {"text": "hi", "intent": {"language": "en"}}}
        )


async def test_default_profile_still_exposes_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exclusion is scoped to the published listing, not to every client."""
    _force_deployment_profile(monkeypatch, None)
    names = [tool.name for tool in await create_server().list_tools()]
    assert "audio.synthesize" in names


def test_the_exclusion_set_names_only_real_tools() -> None:
    """A typo here would silently exclude nothing."""
    known = {tool for tool in action_tools.ACTION_TOOL_NAMES}
    assert CONNECTOR_EXCLUDED_TOOL_NAMES <= known


async def test_dns_rebinding_is_caught_at_the_connected_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-validation resolves public, the connection lands private: refuse anyway.

    This is the check-then-use gap. The body must never be read, let alone
    forwarded to the transcription API.
    """
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    body = b"a-service-account-token"
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(_FakeStream(chunks=[body], peer="169.254.169.254")),
    )

    with pytest.raises(ToolError, match="non-public address"):
        await action_tools._fetch_audio("https://rebind.example.com/a.mp3")


async def test_a_missing_network_stream_does_not_break_the_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live socket to inspect must not become a hard failure."""
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(chunks=[MP3], headers={"content-type": "audio/mpeg"}, peer=None)
        ),
    )

    assert await action_tools._fetch_audio("https://cdn.example.com/rec.mp3") == (MP3, "audio/mpeg")


# --- share links and document responses ------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://drive.google.com/file/d/Abc_123-xyz/view?usp=sharing",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/file/d/Abc_123-xyz/edit",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/file/d/Abc_123-xyz/preview",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/open?id=Abc_123-xyz",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/uc?id=Abc_123-xyz",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/uc?id=Abc_123-xyz&export=view",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://docs.google.com/uc?id=Abc_123-xyz",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/file/d/Abc_123-xyz/view?resourcekey=0-access_key&usp=sharing",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&resourcekey=0-access_key",
        ),
        (
            "https://drive.google.com/open?resourcekey=0-access_key&id=Abc_123-xyz",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&resourcekey=0-access_key",
        ),
        (
            "https://drive.google.com/uc?id=Abc_123-xyz&resourcekey=0-access_key",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&resourcekey=0-access_key",
        ),
        (
            "https://docs.google.com/uc?id=Abc_123-xyz&resourcekey=0-access_key",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&resourcekey=0-access_key",
        ),
        (
            "https://www.dropbox.com/s/abc/recording.mp3?dl=0",
            "https://www.dropbox.com/s/abc/recording.mp3?dl=1",
        ),
        (
            "https://dropbox.com/scl/fi/abc/recording.wav?rlkey=access_key&dl=0",
            "https://dropbox.com/scl/fi/abc/recording.wav?rlkey=access_key&dl=1",
        ),
        (
            "https://www.dropbox.com/scl/fi/abc/recording.wav?rlkey=access_key",
            "https://www.dropbox.com/scl/fi/abc/recording.wav?rlkey=access_key&dl=1",
        ),
        (
            "https://dropbox.com/s/abc/recording.mp3",
            "https://dropbox.com/s/abc/recording.mp3?dl=1",
        ),
        (
            "https://www.dropbox.com/s/abc/recording.mp3?rlkey=access_key&empty=&tag=a&tag=b&dl=0",
            "https://www.dropbox.com/s/abc/recording.mp3?rlkey=access_key&empty=&tag=a&tag=b&dl=1",
        ),
        (
            "https://dl.dropboxusercontent.com/s/abc/recording.mp3?dl=0",
            "https://dl.dropboxusercontent.com/s/abc/recording.mp3?dl=0",
        ),
        (
            "https://cdn.example.com/recording.mp3?signature=secret",
            "https://cdn.example.com/recording.mp3?signature=secret",
        ),
        (
            "https://drive.google.com.attacker.example/file/d/abc/view",
            "https://drive.google.com.attacker.example/file/d/abc/view",
        ),
        (
            "https://dropbox.com.attacker.example/s/abc/recording.mp3?dl=0",
            "https://dropbox.com.attacker.example/s/abc/recording.mp3?dl=0",
        ),
        (
            "https://drive.google.com/file/d/not.a.file.id/view",
            "https://drive.google.com/file/d/not.a.file.id/view",
        ),
        (
            "https://drive.google.com/file/d/Abc_123-xyz?resourcekey=key",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&resourcekey=key",
        ),
        (
            "https://drive.google.com/file/d/Abc_123-xyz/view/",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/file/d/Abc_123-xyz/",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz",
        ),
        (
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&confirm=t&uuid=token",
            "https://drive.google.com/uc?export=download&id=Abc_123-xyz&confirm=t&uuid=token",
        ),
        ("https://drive.google.com/open?id=", "https://drive.google.com/open?id="),
        (
            "https://drive.google.com/file/d/abc/view;extra",
            "https://drive.google.com/file/d/abc/view;extra",
        ),
        ("https://drive.google.com/uc;extra?id=abc", "https://drive.google.com/uc;extra?id=abc"),
        (
            "https://drive.google.com/file/d/abc/view\n",
            "https://drive.google.com/file/d/abc/view\n",
        ),
        (
            "https://drive.google.com\t/file/d/abc/view",
            "https://drive.google.com\t/file/d/abc/view",
        ),
        ("https://[broken", "https://[broken"),
        ("not a URL", "not a URL"),
        ("", ""),
    ],
)
def test_normalize_audio_share_urls(url: str, expected: str) -> None:
    assert action_tools._normalize_audio_url(url) == expected


@pytest.mark.parametrize(
    ("url", "message"),
    [
        *[
            (
                f"https://docs.google.com/{kind}/d/file_id/edit?usp=sharing",
                "That link is a Google Docs document, not an audio file. "
                "Send the link of the audio file itself.",
            )
            for kind in ("document", "spreadsheets", "presentation")
        ],
        *[
            (
                url,
                "That link is a folder. Send the link of one audio file inside it.",
            )
            for url in (
                "https://drive.google.com/drive/folders/folder_id?usp=sharing",
                "https://dropbox.com/sh/folder_id/access_key?dl=0",
                "https://www.dropbox.com/sh/folder_id/access_key?dl=0",
                "https://dropbox.com/scl/fo/folder_id?rlkey=access_key&dl=0",
                "https://www.dropbox.com/scl/fo/folder_id?rlkey=access_key&dl=0",
            )
        ],
    ],
)
async def test_non_audio_share_links_are_rejected_before_fetch(
    monkeypatch: pytest.MonkeyPatch, url: str, message: str
) -> None:
    clients: list[dict[str, Any]] = []

    def unreachable_client(**kwargs: Any) -> Any:
        clients.append(kwargs)
        raise AssertionError("a document or folder must not create an HTTP client")

    monkeypatch.setattr(action_tools.httpx, "AsyncClient", unreachable_client)

    with pytest.raises(ToolError) as error:
        await action_tools.transcribe_audio(url)

    assert str(error.value) == message
    assert clients == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://drive.google.com/file/d/abc/view?resourcekey=access_key",
            "https://drive.google.com/uc?export=download&id=abc&resourcekey=access_key",
        ),
        (
            "https://www.dropbox.com/scl/fi/abc/recording.mp3?rlkey=access_key&dl=0",
            "https://www.dropbox.com/scl/fi/abc/recording.mp3?rlkey=access_key&dl=1",
        ),
    ],
)
async def test_transcribe_fetches_the_normalized_audio_url(
    monkeypatch: pytest.MonkeyPatch, url: str, expected: str
) -> None:
    fetched: list[str] = []

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        fetched.append(url)
        return MP3, "audio/mpeg"

    async def fake_post(*args: Any, **kwargs: Any) -> SpekoRawResponse:
        return SpekoRawResponse(
            content=b'event: done\ndata: {"text": "hello"}\n\n',
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    out = await action_tools.transcribe_audio(url)

    assert fetched == [expected]
    assert out.structured_content == {"text": "hello", "language": "en"}


async def test_google_sign_in_redirect_chain_returns_drive_guidance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    requested: list[str] = []
    resolved: list[str] = []
    options: list[dict[str, Any]] = []
    redirects = [
        "https://drive.google.com/uc?export=download&id=private_file&resourcekey=access_key",
        "https://drive.usercontent.google.com/download?id=private_file&token=secret",
        "https://accounts.google.com/ServiceLogin?continue=secret",
        "https://accounts.google.com/v3/signin/identifier?continue=secret",
    ]

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        resolved.append(host)
        return _public_getaddrinfo()

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a sign-in page must not reach transcription")

    monkeypatch.setattr(action_tools.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            [
                _FakeStream(status_code=303, headers={"location": redirects[1]}),
                _FakeStream(status_code=302, headers={"location": redirects[2]}),
                _FakeStream(status_code=302, headers={"location": redirects[3]}),
                _FakeStream(
                    headers={"content-type": "text/html; charset=utf-8"},
                    chunks=[b"<!doctype html><html>Sign in to Google</html>"],
                ),
            ],
            requested=requested,
            client_options=options,
        ),
    )
    monkeypatch.setattr(http_client, "post_speko_api_bytes", unreachable)
    monkeypatch.setattr(http_client, "post_router_transcription", unreachable)

    with caplog.at_level(logging.WARNING, logger=action_tools.__name__):
        with pytest.raises(ToolError) as error:
            await action_tools.transcribe_audio(
                "https://drive.google.com/file/d/private_file/view?resourcekey=access_key"
            )

    assert str(error.value) == DRIVE_MESSAGE
    assert requested == redirects
    assert resolved == [
        "drive.google.com",
        "drive.usercontent.google.com",
        "accounts.google.com",
        "accounts.google.com",
    ]
    assert options == [{"timeout": 60.0, "follow_redirects": False}]
    assert [record.getMessage() for record in caplog.records] == [
        "audio.transcribe rejected accounts.google.com: document:text/html"
    ]


@pytest.mark.parametrize(
    ("content_type", "body", "kind", "reason"),
    [
        (" Text/HTML ; charset=secret", b"page", "html", "document:text/html"),
        ("application/xhtml+xml", b"page", "html", "document:application/xhtml+xml"),
        ("application/json", b"page", "json", "document:application/json"),
        ("application/xml", b"page", "xml", "document:application/xml"),
        ("text/xml", b"page", "xml", "document:text/xml"),
        ("application/octet-stream", b"<!doctype html><html>", "html", "document:sniffed"),
        ("application/octet-stream", b'{"a": 1}', "json", "document:sniffed"),
        ("application/octet-stream", b'[{"a": 1}]', "json", "document:sniffed"),
        ("application/octet-stream", b"\xef\xbb\xbf \t\r\n<HTML>", "html", "document:sniffed"),
        ("audio/mpeg", b"<head><title>Sign in</title>", "html", "document:sniffed"),
        ("text/plain", b"<body>Sign in", "html", "document:sniffed"),
        ("application/octet-stream", b'<?XML version="1.0"?>', "xml", "document:sniffed"),
        ("application/octet-stream", b'<svg width="20">', "xml", "document:sniffed"),
        ("application/secret-header-value", b"<html>", "html", "document:sniffed"),
    ],
)
async def test_document_responses_are_refused_and_logged_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    content_type: str,
    body: bytes,
    kind: str,
    reason: str,
) -> None:
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(_FakeStream(headers={"content-type": content_type}, chunks=[body])),
    )

    with caplog.at_level(logging.WARNING, logger=action_tools.__name__):
        with pytest.raises(ToolError) as error:
            await action_tools._fetch_audio(
                "https://user:password@cdn.example.com/recording?signature=secret"
            )

    assert str(error.value) == _web_page_message("cdn.example.com", kind)
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("WARNING", f"audio.transcribe rejected cdn.example.com: {reason}")
    ]


@pytest.mark.parametrize("status", [403, 404])
@pytest.mark.parametrize("host", ["drive.google.com", "cdn.example.com"])
async def test_http_errors_report_only_the_host_and_status(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    host: str,
) -> None:
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(_FakeStream(status_code=status, chunks=[b"secret response body"])),
    )

    with caplog.at_level(logging.WARNING, logger=action_tools.__name__):
        with pytest.raises(ToolError) as error:
            await action_tools._fetch_audio(f"https://{host}/recording?signature=secret")

    expected = (
        DRIVE_MESSAGE
        if host == "drive.google.com"
        else (
            f"The link answered HTTP {status} from {host}. "
            "Send a link that downloads the audio directly without a sign-in."
        )
    )
    assert str(error.value) == expected
    assert [record.getMessage() for record in caplog.records] == [
        f"audio.transcribe rejected {host}: http_{status}"
    ]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("accounts.google.com", True),
        ("drive.google.com", True),
        ("drive.usercontent.google.com", True),
        ("docs.google.com", True),
        ("other.google.com", True),
        ("download.googleusercontent.com", True),
        ("nested.download.googleusercontent.com", True),
        ("evilgoogle.com", False),
        ("evilgoogleusercontent.com", False),
        ("google.com.attacker.example", False),
        ("cdn.example.com", False),
        (None, False),
    ],
)
def test_google_host_matching_respects_domain_boundaries(host: str | None, expected: bool) -> None:
    assert action_tools._is_google_host(host) is expected


@pytest.mark.parametrize("host", ["evilgoogle.com", "evilgoogleusercontent.com"])
async def test_lookalike_google_hosts_receive_generic_guidance(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(headers={"content-type": "text/html"}, chunks=[b"<html>Sign in"])
        ),
    )

    with pytest.raises(ToolError) as error:
        await action_tools._fetch_audio(f"https://{host}/recording")

    assert str(error.value) == _web_page_message(host, "html")


@pytest.mark.parametrize("host", ["drive.google.com", "drive.usercontent.google.com"])
async def test_google_virus_confirmation_uses_direct_download_guidance(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    requested: list[str] = []
    body = (
        b"<!DOCTYPE html><html><title>Google Drive - Virus scan warning</title>"
        b"<p>Google Drive can't scan this file for viruses.</p>"
        b'<form id="download-form" action="https://drive.usercontent.google.com/download">'
        b'<input name="confirm" value="secret"><input type="submit" value="Download anyway">'
        b"</form></html>"
    )
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(headers={"content-type": "text/html"}, chunks=[body]),
            requested=requested,
        ),
    )

    with pytest.raises(ToolError) as error:
        await action_tools._fetch_audio(f"https://{host}/download?id=large_file")

    assert str(error.value) == _web_page_message(host, "html")
    assert requested == [f"https://{host}/download?id=large_file"]


@pytest.mark.parametrize(
    "body",
    [
        WAV,
        MP3,
        b"\xff\xfb\x90\x00mp3-frames",
        b"OggS\x00\x02opus-frames",
        b"fLaC\x00\x00flac-frames",
        b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00",
        b"\x1a\x45\xdf\xa3\x9f\x42\x86webm-frames",
        b"<A\x00\x00" + PCM,
        b"<html\x00\x00" + PCM,
        b"<svg\x00\x00" + PCM,
        b'{\xa0"\x00":' + PCM,
    ],
    ids=[
        "wav",
        "id3",
        "mp3-sync",
        "ogg",
        "flac",
        "m4a",
        "webm",
        "pcm-angle-bracket",
        "pcm-html-no-boundary",
        "pcm-svg-no-boundary",
        "pcm-json-latin1-whitespace",
    ],
)
async def test_octet_stream_audio_signatures_are_accepted(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(headers={"content-type": "application/octet-stream"}, chunks=[body])
        ),
    )

    assert await action_tools._fetch_audio("https://cdn.example.com/recording") == (
        body,
        "application/octet-stream",
    )


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/octet-stream", b"<"),
        ("application/octet-stream", b"{"),
        ("application/octet-stream", b"["),
        ("application/octet-stream", b"<A\x00\x00"),
        ("text/plain", MP3),
        ("text/custom", WAV),
        ("application/octet-stream", b'{"' + b"a" * 65 + b'": 1}'),
        ("application/octet-stream", b'{"line\nbreak": 1}'),
        ("application/octet-stream", b"<html\x00\x00" + PCM),
        ("application/octet-stream", b"<svg\x00\x00" + PCM),
        ("application/octet-stream", b'{\xa0"\x00":' + PCM),
        ("application/octet-stream", b"[1,true,null]"),
        ("application/octet-stream", b"<!--" + PCM),
        ("application/octet-stream", b"<!-- unterminated comment " + PCM),
        ("application/octet-stream", b"<!--" + b"x" * 70000 + b"--><html>"),
    ],
)
def test_document_detection_requires_a_matching_label_or_signature(
    content_type: str, body: bytes
) -> None:
    assert action_tools._looks_like_document(content_type, body) is False


@pytest.mark.parametrize(
    ("content_type", "body", "kind"),
    [
        ("application/problem+json", b'{"title": "Access denied"}', "json"),
        ("application/atom+xml", b"<feed/>", "xml"),
        ("application/octet-stream", b" " * 4096 + b"<html>Sign in</html>", "html"),
        ("application/octet-stream", b"\xef\xbb\xbf\r\n<!DOCTYPE html><html>", "html"),
        ("application/octet-stream", b"<html lang=en>", "html"),
        ("application/octet-stream", b"<HEAD>", "html"),
        ("application/octet-stream", b'<?xml version="1.0"?>', "xml"),
        ("application/octet-stream", b"<svg xmlns='x'>", "xml"),
        ("application/octet-stream", b'{"detail": "private"}', "json"),
        ("application/octet-stream", b'[ {"detail": "private"} ]', "json"),
        ("application/octet-stream", b'["Access denied"]', "json"),
        ("application/octet-stream", b'[ "one", "two" ]', "json"),
        ("application/octet-stream", b"<!-- gateway response --><!doctype html>", "html"),
        ("application/octet-stream", b"<!-- a -->\n<!-- b -->\n<html>", "html"),
        ("application/octet-stream", b"<!-- x --><?xml version='1.0'?>", "xml"),
        ("application/octet-stream", b"<!--" + b"x" * 5000 + b"--><html>", "html"),
        (
            "application/octet-stream",
            b"<!--" + b"x" * 3000 + b"-->\n<!--" + b"y" * 2000 + b"--><!doctype html>",
            "html",
        ),
    ],
)
def test_document_detection_names_the_kind(content_type: str, body: bytes, kind: str) -> None:
    assert action_tools._document_kind(content_type, body) == kind


async def test_gzip_document_is_decoded_by_httpx_before_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decoded body guard also catches compressed pages with an audio MIME label."""
    body = b"<!doctype html><html>Sign in</html>"
    real_client = httpx.AsyncClient

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/octet-stream",
                "content-encoding": "gzip",
            },
            content=gzip.compress(body),
        )

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(action_tools.httpx, "AsyncClient", client)

    with pytest.raises(ToolError) as error:
        await action_tools._fetch_audio("https://cdn.example.com/recording?signature=secret")

    assert str(error.value) == _web_page_message("cdn.example.com", "html")


# --- a completed backend response distinguishes silence from interruption ---


@pytest.mark.parametrize("text", ["", " \n\t "])
@pytest.mark.parametrize("metadata", [{}, {"provider": "modulate", "model": "velma"}])
async def test_platform_empty_transcript_has_explicit_no_speech_payload(
    monkeypatch: pytest.MonkeyPatch, text: str, metadata: dict[str, str]
) -> None:
    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return MP3, "audio/mpeg"

    async def fake_post(*args: Any, **kwargs: Any) -> SpekoRawResponse:
        done = json.dumps({"text": text, **metadata})
        return SpekoRawResponse(
            content=(
                'event: transcript\ndata: {"text": "discarded partial", "isFinal": true}\n\n'
                f"event: done\ndata: {done}\n\n"
            ).encode(),
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    out = await action_tools.transcribe_audio(
        "https://cdn.example.com/recording.mp3", language="uz"
    )

    assert out.structured_content == {
        "text": "",
        "language": "uz",
        "no_speech": True,
        "message": NO_SPEECH_MESSAGE,
        **metadata,
    }


@pytest.mark.parametrize(
    "stream",
    [
        b"",
        b'event: meta\ndata: {"provider": "modulate"}\n\n',
        b"event: done\ndata: {}\n\n",
        b'event: done\ndata: {"text": null}\n\n',
        b'event: done\ndata: {"text": 0}\n\n',
        b'event: done\ndata: {"text": false}\n\n',
        b'event: done\ndata: {"text": []}\n\n',
        b'event: done\ndata: {"text": {}}\n\n',
        b"event: done\ndata: not-json\n\n",
    ],
    ids=["empty", "meta-only", "missing-text", "null", "number", "bool", "list", "dict", "invalid"],
)
async def test_transcribe_rejects_an_incomplete_platform_response(
    monkeypatch: pytest.MonkeyPatch, stream: bytes
) -> None:
    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return MP3, "audio/mpeg"

    async def fake_post(*args: Any, **kwargs: Any) -> SpekoRawResponse:
        return SpekoRawResponse(content=stream, content_type="text/event-stream")

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)

    with pytest.raises(ToolError) as error:
        await action_tools.transcribe_audio("https://cdn.example.com/recording.mp3")

    assert str(error.value) == INCOMPLETE_MESSAGE


@pytest.mark.parametrize("text", ["", " \n\t "])
@pytest.mark.parametrize("metadata", [{}, {"provider": "modulate", "model": "velma"}])
async def test_router_empty_transcript_has_explicit_no_speech_payload(
    monkeypatch: pytest.MonkeyPatch, text: str, metadata: dict[str, str]
) -> None:
    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return WAV, "audio/wav"

    async def fake_router(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"text": text, "route": metadata}

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_transcription", fake_router)

    out = await action_tools.transcribe_audio(
        "https://cdn.example.com/recording.wav", language="uz"
    )

    assert out.structured_content == {
        "text": "",
        "language": "uz",
        "no_speech": True,
        "message": NO_SPEECH_MESSAGE,
        **metadata,
    }


@pytest.mark.parametrize(
    "response",
    [{}, {"text": None}, {"text": 0}, {"text": False}, {"text": []}, {"text": {}}],
    ids=["missing", "null", "number", "bool", "list", "dict"],
)
async def test_transcribe_rejects_an_incomplete_router_response(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return WAV, "audio/wav"

    async def fake_router(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return response

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an interrupted Router response must not retry on Platform")

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: "sk_live_test")
    monkeypatch.setattr(http_client, "post_router_transcription", fake_router)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", unreachable)

    with pytest.raises(ToolError) as error:
        await action_tools.transcribe_audio("https://cdn.example.com/recording.wav")

    assert str(error.value) == INCOMPLETE_MESSAGE


async def test_audio_url_description_explains_file_sharing_requirements() -> None:
    tool = next(
        tool for tool in await create_server().list_tools() if tool.name == "audio.transcribe"
    )

    assert tool.parameters["properties"]["audio_url"]["description"] == (
        "HTTPS URL of the audio file (mp3, wav, m4a, ogg, flac, webm; up to 25 MB) "
        "that downloads without a sign-in. Google Drive and Dropbox share links to a FILE "
        "are accepted and converted to direct downloads; in Drive the file must be shared "
        "as 'Anyone with the link'. Signed recording URLs from sessions.recording.get "
        "and calls.recording.get work directly."
    )
    assert "virus" in tool.description.lower()
    assert "confirmation" in tool.description.lower()


# --- credentials stay out of fetch errors and logs -------------------------


@pytest.mark.parametrize("address", ["93.184.216.34", "169.254.169.254"])
def test_fetchable_uses_httpx_idna_host_for_dns(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    """DNS must check the same IDNA name that httpx later connects to."""
    resolved: list[str] = []

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))]

    monkeypatch.setattr(action_tools.socket, "getaddrinfo", fake_getaddrinfo)
    url = "https://faß.example/recording?signature=secret"

    if address == "169.254.169.254":
        with pytest.raises(ToolError, match="non-public address"):
            action_tools._assert_fetchable(url)
    else:
        action_tools._assert_fetchable(url)

    assert resolved == ["xn--fa-hia.example"]


def test_invalid_idna_hosts_raise_a_redacted_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an invalid IDNA host must not reach DNS")

    monkeypatch.setattr(action_tools.socket, "getaddrinfo", unreachable_lookup)

    with pytest.raises(ToolError) as error:
        action_tools._assert_fetchable("https://xn--a.example/a?signature=secret")

    assert "audio_url" in str(error.value)
    assert "signature=secret" not in str(error.value)
    assert "https://xn--a.example" not in str(error.value)


async def test_the_normalized_url_still_passes_through_the_ssrf_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []
    checked: list[str] = []
    assert_fetchable = action_tools._assert_fetchable

    def record_check(url: str) -> None:
        checked.append(url)
        assert_fetchable(url)

    def private_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", 443))
        ]

    monkeypatch.setattr(action_tools, "_assert_fetchable", record_check)
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", private_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(_FakeStream(chunks=[MP3]), requested=requested),
    )

    with pytest.raises(ToolError, match="non-public address"):
        await action_tools.transcribe_audio(
            "https://drive.google.com/file/d/private_file/view?resourcekey=access_key"
        )

    assert checked == [
        "https://drive.google.com/uc?export=download&id=private_file&resourcekey=access_key"
    ]
    assert requested == []


@pytest.mark.parametrize("address", ["169.254.169.254", "not-an-ip?signature=secret"])
@pytest.mark.parametrize("source", ["audio_url", "the address audio_url connected to"])
def test_public_address_errors_do_not_expose_resolved_addresses(address: str, source: str) -> None:
    with pytest.raises(ToolError) as error:
        action_tools._assert_public_address(address, source=source)

    assert str(error.value) == (
        "audio_url resolves to a non-public address. Pass a publicly reachable HTTPS URL, "
        "such as a signed recording URL."
    )


def test_dns_errors_do_not_expose_resolver_details(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_lookup(*args: Any, **kwargs: Any) -> list[Any]:
        raise OSError("resolver-secret: https://user:password@example.com/?signature=secret")

    monkeypatch.setattr(action_tools.socket, "getaddrinfo", failed_lookup)

    with pytest.raises(ToolError) as error:
        action_tools._assert_fetchable("https://cdn.example.com/recording?signature=secret")

    assert str(error.value) == "Unable to resolve the host of audio_url."
    assert "resolver-secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("failure", ["missing-location", "oversized", "empty"])
async def test_fetch_body_and_redirect_errors_report_only_the_host(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    responses = {
        "missing-location": _FakeStream(status_code=302),
        "oversized": _FakeStream(chunks=[b"\x00" * (1024 * 1024)] * 26),
        "empty": _FakeStream(),
    }
    messages = {
        "missing-location": "cdn.example.com redirected without a Location header.",
        "oversized": "Audio at cdn.example.com exceeds the 25 MB limit.",
        "empty": "Audio at cdn.example.com is empty.",
    }
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(action_tools.httpx, "AsyncClient", _client_returning(responses[failure]))

    with pytest.raises(ToolError) as error:
        await action_tools._fetch_audio(
            "https://user:password@cdn.example.com/recording?signature=secret"
        )

    assert str(error.value) == messages[failure]


async def test_fetch_network_errors_do_not_expose_signed_urls(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://user:password@cdn.example.com/recording?signature=secret"
    fake_client = _client_returning(_FakeStream())

    def failed_stream(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError(f"connection-secret for {url}", request=httpx.Request("GET", url))

    monkeypatch.setattr(fake_client, "stream", failed_stream)
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(action_tools.httpx, "AsyncClient", fake_client)

    with pytest.raises(ToolError) as error:
        await action_tools._fetch_audio(url)

    assert str(error.value) == "Unable to fetch audio from cdn.example.com."
    assert "connection-secret" not in "".join(traceback.format_exception(error.value))
    assert "signature=secret" not in caplog.text
    assert "user:password" not in caplog.text


def test_sse_errors_do_not_expose_backend_response_details() -> None:
    detail = "https://user:password@example.com/?signature=secret; X-Token: raw-header"
    stream = f"event: error\ndata: {json.dumps({'error': detail})}\n\n"

    with pytest.raises(ToolError) as error:
        action_tools._transcript_from_sse(stream)

    assert str(error.value) == "Transcription failed. Try again."
    assert "password" not in str(error.value)
    assert "signature=secret" not in str(error.value)


def test_sse_error_codes_outside_the_server_enum_shape_are_dropped() -> None:
    code = "https://user:password@example.com/?signature=secret"
    stream = f"event: error\ndata: {json.dumps({'code': code})}\n\n"

    with pytest.raises(ToolError) as error:
        action_tools._transcript_from_sse(stream)

    assert str(error.value) == "Transcription failed. Try again."


async def test_userinfo_in_audio_url_never_reaches_the_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """urllib raises with the credentials in its message; the host must come from httpx."""
    monkeypatch.setattr(action_tools.socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(
        action_tools.httpx,
        "AsyncClient",
        _client_returning(
            _FakeStream(headers={"content-type": "text/html"}, chunks=[b"<html>Sign in</html>"])
        ),
    )

    with pytest.raises(ToolError) as error:
        await action_tools._fetch_audio(
            "https://user:pass\uff1aword@example.com/audio?signature=secret"
        )

    assert str(error.value) == _web_page_message("example.com", "html")
    assert "pass" not in str(error.value)
    assert "signature" not in str(error.value)


def test_an_error_frame_is_not_reported_as_incomplete() -> None:
    """A failed transcription must never read as a truncated response."""
    stream = (
        'event: error\ndata: {"error": "All providers failed", "code": "ALL_PROVIDERS_FAILED"}\n\n'
    )
    with pytest.raises(ToolError) as error:
        action_tools._transcript_from_sse(stream)
    assert str(error.value) == "Transcription failed (ALL_PROVIDERS_FAILED). Try again."
    assert "All providers failed" not in str(error.value)


@pytest.mark.asyncio
async def test_an_error_frame_without_done_is_a_failure_not_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform sends `error` with HTTP 200; the tool must relay the failure code."""

    async def fake_fetch(url: str) -> tuple[bytes, str]:
        return MP3, "audio/mpeg"

    async def fake_post(*args: Any, **kwargs: Any) -> SpekoRawResponse:
        return SpekoRawResponse(
            content=(
                b"event: error\n"
                b'data: {"error": "provider_unavailable", "code": "ALL_PROVIDERS_FAILED"}\n\n'
            ),
            content_type="text/event-stream",
        )

    monkeypatch.setattr(action_tools, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(http_client, "router_bearer_token", lambda: None)
    monkeypatch.setattr(http_client, "post_speko_api_bytes", fake_post)
    with pytest.raises(ToolError) as error:
        await action_tools.transcribe_audio("https://cdn.example.com/a.mp3")
    assert str(error.value) == "Transcription failed (ALL_PROVIDERS_FAILED). Try again."
