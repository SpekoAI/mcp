"""Tests for the speech synthesis and transcription tools.

Two things are being pinned here:

1. The tools work — correct endpoint, correct body, and a transcript
   assembled out of the SSE frames `/v1/transcribe` actually answers with.
2. `audio.synthesize` is absent from the `?profile=connector` surface while
   `audio.transcribe` stays. Anthropic's Software Directory Policy prohibits
   software that generates audio content, so the published listing omits
   synthesis; transcription returns text and is unaffected. Direct MCP
   clients keep both on the default `/mcp` path.
"""

from __future__ import annotations

import base64
import json
import socket
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import NotFoundError, ToolError

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
from spekoai_mcp.http_client import SpekoRawResponse
from spekoai_mcp.profiles import CONNECTOR_EXCLUDED_TOOL_NAMES, CONNECTOR_PROFILE
from spekoai_mcp.server import create_server

MP3 = b"ID3\x04\x00audio-bytes"


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
        self.headers = headers or {}
        self._chunks = chunks or []
        self.extensions: dict[str, Any] = {}
        if peer is not None:
            self.extensions["network_stream"] = _FakeNetworkStream(peer)

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308)

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self) -> Any:
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _client_returning(response: _FakeStream) -> Any:
    """An httpx.AsyncClient stand-in whose every stream() yields `response`."""

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _FakeStream:
            return response

    return FakeClient


def _force_http_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    """Simulate an HTTP request whose query string carries `profile`."""
    query_params: dict[str, str] = {} if profile is None else {"profile": profile}
    monkeypatch.setattr(
        profiles, "get_http_request", lambda: SimpleNamespace(query_params=query_params)
    )


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
    with pytest.raises(ToolError, match="provider_unavailable"):
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
    _force_http_profile(monkeypatch, CONNECTOR_PROFILE)
    names = [tool.name for tool in await create_server().list_tools()]
    assert "audio.synthesize" not in names
    assert "audio.transcribe" in names


async def test_connector_profile_refuses_to_call_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden must also mean uncallable, or the filter is cosmetic."""
    _force_http_profile(monkeypatch, CONNECTOR_PROFILE)
    with pytest.raises(NotFoundError, match="Unknown tool"):
        await create_server().call_tool(
            "audio.synthesize", {"body": {"text": "hi", "intent": {"language": "en"}}}
        )


async def test_default_profile_still_exposes_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exclusion is scoped to the published listing, not to every client."""
    _force_http_profile(monkeypatch, None)
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

    with pytest.raises(ToolError, match="connected to"):
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
