"""Point "contact support" errors at the support ticket tool.

About fifteen Platform errors end in "Contact support." or "Contact
support@speko.ai" (flag-gated providers, SIP gateway limits, KYB holds,
receptionist runtime, ...), and so does the MCP's own phone-provisioning
``next_step``. A coding agent reads that text and tells the user to go and
write an email, even when the same server offers ``support.ticket.create`` -
which files the ticket with the trace id attached and sends the user a receipt.

This middleware sits under every tool, handwritten and manifest-generated, and
appends one ``support=`` step to exactly those errors. The Platform message is
left as it is, so REST and SDK users keep reading the same text. Profiles that
do not serve the ticket tool are left alone: naming a tool the client cannot
call dead-ends on "Unknown tool".
"""

from __future__ import annotations

import re

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mt

from spekoai_mcp.http_client import SpekoApiError
from spekoai_mcp.profiles import current_profile, profile_serves_tool

SUPPORT_TICKET_TOOL = "support.ticket.create"

# "Contact support.", "contact Speko support", "Contact support@speko.ai".
_CONTACT_SUPPORT = re.compile(r"contact (?:speko )?support|support@speko\.ai", re.IGNORECASE)


def support_next_step(trace_id: str | None) -> str:
    trace = f"trace_id {trace_id}" if trace_id else "the trace_id above"
    return (
        "When this needs the Speko team (after any fix named above has been tried), offer "
        f"to open a ticket with {SUPPORT_TICKET_TOOL}: a one-line summary, what the user "
        f"was doing and this error in details ({trace} included), and any agentId, "
        "sessionId, callId or phoneNumber in related. The team replies to the user's "
        "email. Point the user to support@speko.ai only if the ticket tool itself fails."
    )


class SupportHandoffMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, object],
    ) -> object:
        try:
            return await call_next(context)
        except Exception as exc:
            message = str(exc)
            if (
                context.message.name == SUPPORT_TICKET_TOOL
                or not _CONTACT_SUPPORT.search(message)
                or "support=" in message
                or not profile_serves_tool(SUPPORT_TICKET_TOOL, current_profile())
            ):
                raise
            step = support_next_step(_trace_id(exc))
            raise ToolError(f"{message}; support={step}") from exc


def _trace_id(exc: BaseException) -> str | None:
    """The trace id of the ``SpekoApiError`` behind ``exc``, if there is one.

    FastMCP wraps what a tool raises as ``ToolError(...) from exc``, so the API
    error is usually one link down the ``__cause__`` chain.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SpekoApiError):
            return current.trace_id
        current = current.__cause__ or current.__context__
    return None
