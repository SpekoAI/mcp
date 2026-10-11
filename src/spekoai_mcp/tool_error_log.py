"""One WARNING line per failed tool call, carrying the reason.

FastMCP logs a ``ToolError`` as a bare ``Error calling tool 'sessions.phone.create'``
with no status, code or message (``exc_info=False``). In the 2026-10-04..11
prod audit that was every user-facing MCP failure: the reason reached the
model and nowhere else, so two open issues could not be root-caused from the
logs. This middleware sits under every tool, handwritten and manifest-generated,
and writes the missing line once.

What it logs: the tool name, the Platform HTTP status, the error ``code``, the
trace id (Platform's request id, which joins this line to the server's own
``[warn] [<request_id>]`` line) and the message, truncated. It never logs tool
arguments, request bodies or tokens; URL query strings (signed recording links)
and E.164 numbers inside the message are masked first.
"""

from __future__ import annotations

import logging
import re

from fastmcp.exceptions import ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mt

from spekoai_mcp.http_client import SpekoApiError, SpekoAuthError

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 300

# A signed recording URL's query string is a credential (see __main__'s httpx
# level), and Platform messages can echo URLs back. Keep the host and path.
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*", re.IGNORECASE)
# A dialed number next to a workspace id is personal data. Keep the last four
# digits so a support thread can still be matched to a line.
_E164 = re.compile(r"\+\d{7,15}")


class ToolErrorLogMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, object],
    ) -> object:
        try:
            return await call_next(context)
        except Exception as exc:
            log_tool_error(context.message.name, exc)
            raise


def log_tool_error(tool: str, exc: BaseException) -> None:
    # Argument-validation failures are already logged by FastMCP with their
    # detail ("Invalid arguments for tool ..."); a second line adds nothing.
    if isinstance(exc, ValidationError):
        return
    api_error = _api_error(exc)
    if api_error is None and not isinstance(exc, ToolError):
        # Unknown tool, disabled tool and the like: not a tool failure.
        return
    if isinstance(api_error, SpekoApiError):
        status: int | None = api_error.status_code
        code = api_error.code
        trace_id = api_error.trace_id
        message = api_error.message
    else:
        status = None
        code = "MCP_AUTH" if isinstance(api_error, SpekoAuthError) else None
        trace_id = None
        message = str(api_error or exc)
    logger.warning(
        "tool_error tool=%s status=%s code=%s trace_id=%s message=%s",
        tool,
        status if status is not None else "-",
        code or "-",
        trace_id or "-",
        safe_message(message),
    )


def safe_message(message: str) -> str:
    text = " ".join(message.split())
    text = _URL_QUERY.sub(r"\1?[redacted]", text)
    text = _E164.sub(lambda m: f"+***{m.group(0)[-4:]}", text)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 3] + "..."
    return text


def _api_error(exc: BaseException) -> SpekoApiError | SpekoAuthError | None:
    """The Platform error behind ``exc``, if there is one.

    FastMCP wraps what a tool raises as ``ToolError(...) from exc`` and
    ``action_tools.tool_error`` re-raises with ``from exc``, so the API error is
    usually one or two links down the ``__cause__`` chain.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (SpekoApiError, SpekoAuthError)):
            return current
        current = current.__cause__ or current.__context__
    return None
