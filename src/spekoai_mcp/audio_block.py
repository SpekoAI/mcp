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
with no identity, gets the result unchanged: the block repeats the clip, so it
doubles the response. A clip above 5 MiB gets no block for the same reason.
"""

from __future__ import annotations

from fastmcp.tools import ToolResult
from mcp.types import AudioContent

from spekoai_mcp.http_client import current_anthropic_client, current_client_info_name

AUDIO_BLOCK_ANTHROPIC_CLIENTS = frozenset({"cowork", "claudecode"})
AUDIO_BLOCK_CLIENT_INFO_NAME = "claude-code"

# The block repeats `audio_base64`, so it doubles the response. Claude Code
# does not read an MCP response larger than 16 MB: the agent then gets an error
# with no audio and no link (seen on 2.1.322 with a 7 MB clip, an 18.6 MB
# response). 5 MiB of audio is about 14 MB as two base64 copies. A larger clip
# keeps the result it had before, with the link.
_MAX_AUDIO_BLOCK_BYTES = 5 * 1024 * 1024

# Headerless PCM is not a file a client can open once it is saved.
_UNPLAYABLE_MIME_TYPES = frozenset({"audio/pcm", "audio/l16"})


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


def with_audio_block(result: ToolResult) -> ToolResult:
    """Return ``result``, with its clip appended as an `audio` block for Claude Code hosts."""
    try:
        if not client_takes_audio_block():
            return result
        payload = result.structured_content
        if not isinstance(payload, dict):
            return result
        data = payload.get("audio_base64")
        if not isinstance(data, str) or not data:
            return result
        decoded_size = len(data) // 4 * 3 - data[-2:].count("=")
        if decoded_size > _MAX_AUDIO_BLOCK_BYTES:
            return result
        content_type = payload.get("content_type")
        if not isinstance(content_type, str):
            return result
        mime_type = content_type.split(";", 1)[0].strip().lower()
        if not mime_type.startswith("audio/") or mime_type in _UNPLAYABLE_MIME_TYPES:
            return result
        return ToolResult(
            content=[*result.content, AudioContent(type="audio", data=data, mime_type=mime_type)],
            structured_content=payload,
            meta=result.meta,
            is_error=result.is_error,
        )
    except Exception:
        # The block is an extra copy of the clip; it must never fail a synthesis.
        return result
