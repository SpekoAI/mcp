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

import h11
import pytest

from spekoai_mcp import http_client
from spekoai_mcp.http_client import (
    ANTHROPIC_CLIENT_MAX_LENGTH,
    CLIENT_INFO_NAME_MAX_LENGTH,
    _platform_headers,
    reset_current_anthropic_client,
    reset_current_client_info,
    reset_current_client_ua,
    set_current_anthropic_client,
    set_current_client_info,
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

    # A cut landing on a space must still be a sendable header value.
    spaced = {"headers": [(b"user-agent", b"a" * (USER_AGENT_MAX_LENGTH - 1) + b" tail")]}
    user_agent = MCPProtocolGuard._sanitized_user_agent(spaced)
    assert user_agent == user_agent.strip()
    h11.Request(
        method="GET", target="/", headers=[("Host", "x"), ("X-Speko-Client-UA", user_agent)]
    )


def test_self_report_headers_absent_when_nothing_was_bound() -> None:
    """Absent, not empty, for the same reason as the UA."""
    headers = _platform_headers()
    for name in (
        "X-Speko-Anthropic-Client",
        "X-Speko-Client-Info-Name",
        "X-Speko-Client-Info-Version",
        "X-Speko-Client-Info-Title",
    ):
        assert name not in headers


def test_anthropic_client_header_forwarded_when_bound() -> None:
    scope = {"headers": [(b"x-anthropic-client", b"ClaudeCode")]}
    token = set_current_anthropic_client(MCPProtocolGuard._anthropic_client(scope))
    try:
        headers = _platform_headers()
    finally:
        reset_current_anthropic_client(token)

    assert headers["X-Speko-Anthropic-Client"] == "ClaudeCode"


def test_anthropic_client_is_bounded_and_printable() -> None:
    hostile = {"headers": [(b"x-anthropic-client", b"Claude\x00Code" + b"x" * 200)]}
    value = MCPProtocolGuard._anthropic_client(hostile)
    assert "\x00" not in value
    assert len(value) == ANTHROPIC_CLIENT_MAX_LENGTH
    assert MCPProtocolGuard._anthropic_client({"headers": []}) == ""


def test_client_info_forwarded_field_by_field() -> None:
    token = set_current_client_info({"name": "claude-code", "version": "2.1.0", "title": ""})
    try:
        headers = _platform_headers()
    finally:
        reset_current_client_info(token)

    assert headers["X-Speko-Client-Info-Name"] == "claude-code"
    assert headers["X-Speko-Client-Info-Version"] == "2.1.0"
    # A blank field is not forwarded as an empty header.
    assert "X-Speko-Client-Info-Title" not in headers


@pytest.mark.parametrize(
    "client_info",
    [None, "claude-code", {"name": 42, "version": ["2"]}, {"name": "   "}],
)
def test_client_info_of_the_wrong_shape_forwards_nothing(client_info: object) -> None:
    token = set_current_client_info(client_info)
    try:
        headers = _platform_headers()
    finally:
        reset_current_client_info(token)

    assert not [name for name in headers if name.startswith("X-Speko-Client-Info-")]


@pytest.mark.parametrize("value", ["a" * 127 + " b", "  padded  ", "a\nb"])
def test_bounded_values_are_always_sendable_header_values(value: str) -> None:
    """A cut landing on a space must not leave trailing whitespace: httpx
    rejects it as an illegal header value and the tool call would fail."""
    token = set_current_client_info({"name": value})
    try:
        headers = _platform_headers()
    finally:
        reset_current_client_info(token)

    # h11 is the layer that rejects an illegal value; it validates on construction.
    h11.Request(method="GET", target="/", headers=[("Host", "platform.invalid"), *headers.items()])
    name = headers["X-Speko-Client-Info-Name"]
    assert name == name.strip()


def test_client_info_is_bounded_ascii() -> None:
    token = set_current_client_info({"name": "Claude ✨ Code\n" + "n" * 300})
    try:
        name = _platform_headers()["X-Speko-Client-Info-Name"]
    finally:
        reset_current_client_info(token)

    assert name.isascii() and name.isprintable()
    assert len(name) == CLIENT_INFO_NAME_MAX_LENGTH
    assert name.startswith("Claude ? Code")
