"""Tool descriptions and server instructions fit Claude Code's 2 KB fields.

Claude Code cut both at 2,048 characters before v2.1.296 and the model saw
only the text before the cut. Clients older than that still apply it, and
Manufact's publishing checklist (`claude-code-metadata-length`) still scores against the
2 KB cap. `agents.test_call` ran to 2,266 bytes before it was condensed.
Measured in UTF-8 bytes, the stricter reading, so an em dash cannot slip a
description over the line.
"""

from __future__ import annotations

import pytest

from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR, KNOWN_PROFILES
from spekoai_mcp.server import create_public_server, create_server

CLAUDE_CODE_FIELD_BYTES = 2048


def _size(text: str | None) -> int:
    return len((text or "").encode("utf-8"))


@pytest.mark.parametrize("profile", [None, *sorted(KNOWN_PROFILES)])
async def test_every_profile_fits_claude_code_fields(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    mcp = create_server()

    assert _size(mcp.instructions) <= CLAUDE_CODE_FIELD_BYTES
    oversized = {
        tool.name: _size(tool.description)
        for tool in await mcp.list_tools()
        if _size(tool.description) > CLAUDE_CODE_FIELD_BYTES
    }
    assert oversized == {}


async def test_public_server_fits_claude_code_fields() -> None:
    mcp = create_public_server()

    assert _size(mcp.instructions) <= CLAUDE_CODE_FIELD_BYTES
    oversized = {
        tool.name: _size(tool.description)
        for tool in await mcp.list_tools()
        if _size(tool.description) > CLAUDE_CODE_FIELD_BYTES
    }
    assert oversized == {}
