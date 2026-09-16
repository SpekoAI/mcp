"""The harness identity forwarded to Platform as `X-Speko-Client-UA`.

`X-Speko-Client` carries an OAuth client_id (or the literal
"unknown-mcp-client") and is an auth identity by construction — it can never
say "Claude Code". This header is the separate channel that can, and Platform
maps it to a readable `mcp_harness` bucket.

These pin the three properties the platform side depends on: the header appears
when a User-Agent was bound, it is ABSENT rather than empty when one was not,
and the sanitizer bounds what can ever be bound.
"""

from __future__ import annotations

import pytest

from spekoai_mcp import http_client
from spekoai_mcp.http_client import (
    _platform_headers,
    reset_current_client_ua,
    set_current_client_ua,
)
from spekoai_mcp.server import USER_AGENT_MAX_LENGTH, MCPProtocolGuard


@pytest.fixture(autouse=True)
def _bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_platform_headers` requires an authenticated context; stub it."""
    monkeypatch.setattr(http_client, "_bearer_token", lambda: "sk_platform_test")


def test_header_forwarded_when_user_agent_is_bound() -> None:
    token = set_current_client_ua("claude-code/1.2.3")
    try:
        headers = _platform_headers()
    finally:
        reset_current_client_ua(token)

    assert headers["X-Speko-Client-UA"] == "claude-code/1.2.3"


def test_header_absent_when_no_user_agent() -> None:
    """Absent, not empty.

    Platform distinguishes "no UA header" (mcp_harness = 'unknown') from "UA
    present but unrecognised" (mcp_harness = 'other'). Emitting an empty string
    would collapse those two buckets into one.
    """
    assert "X-Speko-Client-UA" not in _platform_headers()


def test_auth_identity_header_is_untouched() -> None:
    """`X-Speko-Client` keeps its old meaning.

    It is persisted as actionExecution.clientName, so redefining it would
    silently change a live column's meaning for every historical row.
    """
    token = set_current_client_ua("cursor/0.44")
    try:
        headers = _platform_headers()
    finally:
        reset_current_client_ua(token)

    assert headers["X-Speko-Client"] == "unknown-mcp-client"
    assert headers["X-Speko-Client-UA"] == "cursor/0.44"


def test_sanitizer_truncates_and_strips_unprintables() -> None:
    """The bound value is bounded at the ASGI edge, where it is read.

    Truncation lives in the sanitizer rather than at the header, so a hostile
    User-Agent cannot reach Platform unbounded regardless of which caller binds
    it.
    """
    raw = ("a" * (USER_AGENT_MAX_LENGTH * 2)).encode()
    scope = {"headers": [(b"user-agent", raw)]}

    sanitized = MCPProtocolGuard._sanitized_user_agent(scope)

    assert len(sanitized) == USER_AGENT_MAX_LENGTH

    control = {"headers": [(b"user-agent", b"cursor/\x00\x01 0.44")]}
    assert "\x00" not in MCPProtocolGuard._sanitized_user_agent(control)
