"""Each tool is registered once, and the served tools did not change.

Every instance start used to log `Component already exists: tool:<id>@` for
the 13 ids that have both a handwritten relay and a manifest action.
FastMCP's `on_duplicate="warn"` replaced the handwritten copy in place, so
clients got the manifest tool at the handwritten tool's list position.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

import pytest
from fastmcp import FastMCP

import spekoai_mcp.server as server
from spekoai_mcp.action_tools import ACTION_TOOL_NAME_BY_FUNCTION, register_action_tools
from spekoai_mcp.generated_action_tools import _MANIFEST_ACTION_IDS
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR, KNOWN_PROFILES

PROFILES = [None, *sorted(KNOWN_PROFILES)]


def _set_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)


async def _served(mcp: FastMCP) -> list[dict[str, Any]]:
    return [tool.to_mcp_tool().model_dump(mode="json") for tool in await mcp.list_tools()]


def test_some_handwritten_relays_share_an_id_with_the_manifest() -> None:
    # Without an overlap the tests below prove nothing.
    assert set(ACTION_TOOL_NAME_BY_FUNCTION.values()) & _MANIFEST_ACTION_IDS


@pytest.mark.parametrize("profile", PROFILES)
def test_every_server_builds_with_duplicates_as_errors(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    _set_profile(monkeypatch, profile)
    monkeypatch.setattr(server, "FastMCP", functools.partial(FastMCP, on_duplicate="error"))
    server.create_server()
    server.create_public_server()


def test_server_start_logs_no_duplicate_warning(caplog: pytest.LogCaptureFixture) -> None:
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Capture(level=logging.WARNING)
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(handler)
    try:
        server.create_server()
        server.create_public_server()
    finally:
        fastmcp_logger.removeHandler(handler)
    assert not [r for r in records if "already exists" in r.getMessage()]


@pytest.mark.parametrize("profile", PROFILES)
async def test_served_tools_match_the_old_replace_registration(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    """Same names, order and definitions as the old register-then-replace."""
    _set_profile(monkeypatch, profile)
    current = await _served(server.create_server())

    def register_all_handwritten(mcp: FastMCP, **_: Any) -> frozenset[str]:
        register_action_tools(mcp)
        return frozenset()

    monkeypatch.setattr(server, "register_action_tools", register_all_handwritten)
    monkeypatch.setattr(server, "FastMCP", functools.partial(FastMCP, on_duplicate="replace"))
    legacy = await _served(server.create_server())

    assert [tool["name"] for tool in current] == [tool["name"] for tool in legacy]
    assert current == legacy
