"""After an outbound call with no agent, say that callbacks can leave a voicemail.

A Speko number that has no agent attached now takes a voicemail when someone
calls it (Platform `inbound-voicemail` remote config): the caller hears a
greeting, leaves a message, and Speko emails the recording and transcript to
the workspace's owners and admins. Before that, the call was refused. Most of
those calls are the person a chat client just dialed ringing the number back
(42 of 101 refusals, prod 2026-10-19..21), so the notice rides on the
``sessions.phone.create`` result for a call placed without an ``agentId``,
which is the case where the calling number has no agent to answer.

Only fires when all of these hold:

- ``SPEKO_MCP_VOICEMAIL_NOTICE`` is ``true``/``1`` (default off),
- the call was created (not an error) and the request had no ``agentId``,
- the request carries an organization id,
- this server process has not shown the notice to that organization in the
  last ``NOTICE_INTERVAL_SECONDS``.

The once-per-interval memory is per process, so a workspace whose requests
land on several instances can see it once per instance; it is never per call.
The greeting sentence is added only where the session can call
``phone_numbers.update``, so the line never names a tool the host does not
serve. Each shown notice logs one ``mcp_notice_exposure`` record on the
attribution log transport.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from spekoai_mcp import attribution

VOICEMAIL_NOTICE_ENV_VAR = "SPEKO_MCP_VOICEMAIL_NOTICE"
NOTICE_ID = "inbound_voicemail_v1"
GREETING_TOOL = "phone_numbers.update"
NOTICE_INTERVAL_SECONDS = 7 * 24 * 60 * 60
_MAX_REMEMBERED_ORGS = 50_000

# Tool results follow the same rule as tool descriptions: state facts, never
# direct the model.
VOICEMAIL_NOTICE_TEXT = (
    "If someone calls this Speko number back and no agent is set to answer it, "
    "they can leave a voicemail. Speko emails the recording and transcript to "
    "the workspace's owners and admins."
)
VOICEMAIL_GREETING_TEXT = (
    "The greeting callers hear can be changed with voicemailGreeting in phone_numbers.update."
)


def notice_enabled() -> bool:
    return os.environ.get(VOICEMAIL_NOTICE_ENV_VAR, "").strip().lower() in {"true", "1"}


class _ShownLog:
    """Organizations shown the notice recently, bounded and thread-safe."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._shown: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, organization_id: str) -> bool:
        """True (and remembered) when this org has not been shown it within the interval."""
        now = self._clock()
        with self._lock:
            last = self._shown.get(organization_id)
            if last is not None and now - last < NOTICE_INTERVAL_SECONDS:
                return False
            self._shown[organization_id] = now
            self._shown.move_to_end(organization_id)
            while len(self._shown) > _MAX_REMEMBERED_ORGS:
                self._shown.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._shown.clear()


_SHOWN = _ShownLog()


def _organization_id() -> str | None:
    claims = getattr(get_access_token(), "claims", None)
    if not isinstance(claims, dict):
        return None
    value = claims.get("organization_id")
    return value if isinstance(value, str) and value else None


def _session_can_set_greeting() -> bool:
    # Imported at call time: profiles imports the action manifest.
    from spekoai_mcp.profiles import session_can_call

    return session_can_call(GREETING_TOOL)


def notice_text(*, can_set_greeting: bool) -> str:
    if can_set_greeting:
        return f"{VOICEMAIL_NOTICE_TEXT} {VOICEMAIL_GREETING_TEXT}"
    return VOICEMAIL_NOTICE_TEXT


def with_voicemail_notice(result: ToolResult, body: Mapping[str, Any]) -> ToolResult:
    """Return ``result``, with the voicemail line appended when this call should show it."""
    try:
        if not notice_enabled() or result.is_error:
            return result
        if body.get("agentId"):
            # The number answers with that agent, not voicemail.
            return result
        organization_id = _organization_id()
        if organization_id is None or not _SHOWN.claim(organization_id):
            return result
        can_set_greeting = _session_can_set_greeting()
        attribution.submit_event(
            {
                "event": "mcp_notice_exposure",
                "event_version": 1,
                "notice": NOTICE_ID,
                "organization_id": organization_id,
                "tool": "sessions.phone.create",
                "greeting_tool": can_set_greeting,
                "invocation_id": attribution.current_invocation_id(),
                "exposed_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            }
        )
        return _append_line(result, notice_text(can_set_greeting=can_set_greeting))
    except Exception:
        # A notice must never fail or otherwise change a call that was placed.
        return result


def _append_line(result: ToolResult, line: str) -> ToolResult:
    content = list(result.content)
    for index, block in enumerate(content):
        if isinstance(block, TextContent):
            content[index] = TextContent(type="text", text=f"{block.text}\n{line}")
            break
    else:
        content.append(TextContent(type="text", text=line))
    return ToolResult(
        content=content,
        structured_content=result.structured_content,
        meta=result.meta,
        is_error=result.is_error,
    )
