"""One out-of-credit answer for every tool.

Platform's pre-flight credit gates refuse new work with ``402
INSUFFICIENT_CREDITS`` once a workspace balance reaches zero. The handwritten
dotted tools already convert ``SpekoApiError`` into a ``ToolError`` (see
``action_tools.call``); the audio tools and every manifest-generated tool call
``http_client`` directly and let the exception escape, so FastMCP rendered it
as ``Error calling tool 'audio.synthesize': Speko API returned 402: ...`` with
no remedy and no billing link. This middleware sits under all of them and turns
that one failure into the same message ``tool_error_message`` produces, so a
Claude Code / Codex / Claude.ai user reads the same thing regardless of which
tool ran out of credit first.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import types as mt

from spekoai_mcp.http_client import BILLING_URL, SpekoApiError, credit_exhausted_message

BALANCE_ACTION_ID = "credits.balance.get"


class CreditExhaustedMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, object],
    ) -> object:
        try:
            return await call_next(context)
        except Exception as exc:
            api_error = _credit_exhaustion(exc)
            if api_error is None:
                raise
            raise ToolError(credit_exhausted_message(api_error)) from api_error


def _credit_exhaustion(exc: BaseException) -> SpekoApiError | None:
    """The out-of-credit ``SpekoApiError`` behind ``exc``, if that is what it is.

    FastMCP's tool runner wraps anything a tool raises as
    ``ToolError("Error calling tool ...") from exc`` before the middleware chain
    sees it, so the interesting exception is usually one link down the
    ``__cause__`` chain, not the one caught.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SpekoApiError):
            return current if current.credit_exhausted else None
        current = current.__cause__ or current.__context__
    return None


def balance_result_text(payload: dict[str, Any], default: str) -> str:
    """Result text for ``credits.balance.get``.

    A balance at or below zero is not just a number: Platform is refusing new
    sessions, calls and audio requests. Say so where the model reads it, with
    the billing page, so a "check my balance" question ends in a remedy.
    """
    balance = _balance_usd(payload)
    if balance is None or balance > 0:
        return default
    return (
        f"Credit balance is ${balance:.2f}. This workspace is out of credit, so new sessions, "
        "calls and audio requests are refused until a workspace owner or admin adds credit at "
        f"{BILLING_URL}.\n{default}"
    )


def _balance_usd(payload: dict[str, Any]) -> float | None:
    # /v1/actions/credits.balance.get answers the action output, {kind: "result",
    # data: {balanceUsd}}; the REST route answers {balanceUsd} flat. Accept both.
    candidates: list[Any] = [payload, payload.get("data")]
    for candidate in candidates:
        if isinstance(candidate, dict):
            value = candidate.get("balanceUsd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None
