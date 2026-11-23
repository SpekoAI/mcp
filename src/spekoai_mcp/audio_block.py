"""Return a synthesized clip as an MCP `audio` content block to Claude Code hosts.

`audio.synthesize` carries its audio as base64 in `structuredContent`. Cowork
gives its agent only the `content` blocks of a tool result, so that agent is
left with the download link, and some Cowork sandboxes cannot reach the link's
host (three tickets on 2026-11-10). Claude Code saves an `audio` content block
to a file and tells the agent the path, which needs neither `structuredContent`
nor network access.

Only for clients on the Claude Code engine: Anthropic's `x-anthropic-client`
header is `Cowork` or `ClaudeCode`, or, when the client sends no such header,
the MCP `clientInfo.name` is `claude-code`. Every other client, and a client
with no identity, gets the result unchanged.

Claude Code does not read an MCP response larger than 16 MB: the agent then
gets an error with no audio and no link (seen on 2.1.322 with a 7 MB clip sent
twice, an 18.6 MB response). So the size of the clip decides what is sent:

- up to 5 MiB: the block and `audio_base64`, about 14 MB at most;
- up to 8 MiB, when the result has a download link: the block alone, about
  11 MB at most, with `audio_base64` left out of `structuredContent`. The
  bundled player then offers the link. A result with no link keeps
  `audio_base64` and gets no block;
- above that: no block. `audio.synthesize` says so in its text.
"""

from __future__ import annotations

import base64
import logging

from fastmcp.tools import ToolResult
from mcp.types import AudioContent, TextContent

from spekoai_mcp.http_client import current_anthropic_client, current_client_info_name

logger = logging.getLogger(__name__)

AUDIO_BLOCK_ANTHROPIC_CLIENTS = frozenset({"cowork", "claudecode"})
AUDIO_BLOCK_CLIENT_INFO_NAME = "claude-code"

# The largest clip sent as a block, and the largest sent beside `audio_base64`.
MAX_AUDIO_BLOCK_BYTES = 8 * 1024 * 1024
_MAX_TWO_COPY_BYTES = 5 * 1024 * 1024

# Headerless PCM is not a file a client can open once it is saved.
_UNPLAYABLE_MIME_TYPES = frozenset({"audio/pcm", "audio/l16"})

# Added to the `audio.synthesize` text for a clip too large to attach. States
# the limit and what stays inside it; tool results never direct the model.
AUDIO_BLOCK_TOO_LARGE_TEXT = (
    "This clip is larger than 8 MB, so it is not attached as an audio file; the "
    "download link is the only copy. A clip of up to about 2.5 minutes of speech "
    "is attached as a file."
)


def client_takes_audio_block() -> bool:
    """Whether the calling client is on the Claude Code engine.

    The header decides when it is present: claude.ai can send the same
    `clientInfo.name` as Claude Code, and `ClaudeAI` must not get the block.
    The name counts only for a client that sends no header, such as the CLI.
    """
    anthropic_client = (current_anthropic_client() or "").strip().lower()
    if anthropic_client:
        return anthropic_client in AUDIO_BLOCK_ANTHROPIC_CLIENTS
    return (current_client_info_name() or "").strip().lower() == AUDIO_BLOCK_CLIENT_INFO_NAME


def audio_block_mime_type(content_type: object) -> str | None:
    """The block's MIME type for a clip a client can open as a file, else None."""
    if not isinstance(content_type, str):
        return None
    mime_type = content_type.split(";", 1)[0].strip().lower()
    if not mime_type.startswith("audio/") or mime_type in _UNPLAYABLE_MIME_TYPES:
        return None
    return mime_type


def with_audio_block(result: ToolResult) -> ToolResult:
    """Return an `audio.synthesize` ``result`` with its clip as an `audio` block.

    Only for Claude Code hosts. A clip too large to send twice loses
    `audio_base64`; a clip too large to attach gets a line saying so.
    """
    try:
        if not client_takes_audio_block():
            return result
        payload = result.structured_content
        if not isinstance(payload, dict):
            return result
        mime_type = audio_block_mime_type(payload.get("content_type"))
        if mime_type is None:
            return result
        data = payload.get("audio_base64")
        if not isinstance(data, str) or not data:
            size = payload.get("size_bytes")
            if isinstance(size, int) and size > MAX_AUDIO_BLOCK_BYTES and "url" in payload:
                return _with_text_line(result, AUDIO_BLOCK_TOO_LARGE_TEXT)
            return result
        size = len(data) // 4 * 3 - data[-2:].count("=")
        if size > MAX_AUDIO_BLOCK_BYTES:
            return result
        if size > _MAX_TWO_COPY_BYTES:
            # The bundled player reads `structuredContent` only. Without
            # `audio_base64` it falls back to the link, so a result with no
            # link (the Router and streamed paths) keeps what it had.
            url = payload.get("url")
            if not isinstance(url, str) or not url.startswith("https://"):
                return result
            payload = {key: value for key, value in payload.items() if key != "audio_base64"}
        return _with_block(
            result, data, mime_type, structured_content=payload, size=size, tool="audio.synthesize"
        )
    except Exception:
        # The block is an extra copy of the clip; it must never fail a synthesis.
        return result


def with_stored_audio_block(result: ToolResult, audio: bytes, content_type: object) -> ToolResult:
    """Return an `audio.synthesis.get` ``result`` with the stored clip as an `audio` block.

    The caller checks `client_takes_audio_block()` before it reads the clip, so
    no other client costs a download. `structuredContent` stays as it is.
    """
    try:
        mime_type = audio_block_mime_type(content_type)
        if mime_type is None or not 0 < len(audio) <= MAX_AUDIO_BLOCK_BYTES:
            return result
        return _with_block(
            result,
            base64.b64encode(audio).decode("ascii"),
            mime_type,
            structured_content=result.structured_content,
            size=len(audio),
            tool="audio.synthesis.get",
        )
    except Exception:
        return result


def _with_block(
    result: ToolResult,
    data: str,
    mime_type: str,
    *,
    structured_content: object,
    size: int,
    tool: str,
) -> ToolResult:
    # The gate fails closed: a renamed header value stops the block with no
    # error. This line is how the block's use is seen in production.
    logger.info(
        "audio_block_attached tool=%s anthropic_client=%r client_info_name=%r bytes=%d",
        tool,
        current_anthropic_client() or "",
        current_client_info_name() or "",
        size,
        extra={"event": "audio_block_attached", "tool": tool, "audio_bytes": size},
    )
    return ToolResult(
        content=[*result.content, AudioContent(type="audio", data=data, mime_type=mime_type)],
        structured_content=structured_content,
        meta=result.meta,
        is_error=result.is_error,
    )


def _with_text_line(result: ToolResult, line: str) -> ToolResult:
    content = list(result.content)
    for index, block in enumerate(content):
        if isinstance(block, TextContent):
            content[index] = TextContent(type="text", text=f"{block.text}\n{line}")
            break
    else:
        return result
    return ToolResult(
        content=content,
        structured_content=result.structured_content,
        meta=result.meta,
        is_error=result.is_error,
    )
