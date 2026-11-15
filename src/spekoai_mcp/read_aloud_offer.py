"""After Claude reads something aloud, mention that Speko can also call.

Growth experiment: a claude.ai user who just heard ``audio.synthesize`` output
learns, in one plain line of the tool result, that Speko can read it on a call
to their own phone or call a business for them. Half of organizations get the
line (``offer``) and half get nothing (``holdout``); the arm is a stable hash of
the organization id, so an org never flips arms and the arm can be recomputed
from the id alone when analysing.

Only fires when all of these hold:

- ``SPEKO_MCP_READ_ALOUD_OFFER`` is ``true``/``1`` (default off),
- the caller's User-Agent classifies as ``claude_hosted`` (claude.ai),
- the request carries an organization id,
- this session can call ``sessions.phone.create``, so the line never points at
  a tool the deployment does not serve.

The Anthropic directory host (connector profile) does not serve
``audio.synthesize`` at all, so this never runs there.

Every eligible call logs one ``mcp_experiment_exposure`` record on the
attribution log transport, holdout included; ineligible calls log nothing.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from typing import Literal

from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from spekoai_mcp import attribution
from spekoai_mcp.client_classification import classify_execution_client
from spekoai_mcp.http_client import current_client_ua

READ_ALOUD_OFFER_ENV_VAR = "SPEKO_MCP_READ_ALOUD_OFFER"
EXPERIMENT_ID = "read_aloud_call_offer_v1"
ELIGIBLE_CLIENT = "claude_hosted"
PHONE_CALLING_TOOL = "sessions.phone.create"

# The one line appended to the result text in the offer arm. Wording needs
# Bek's OK before the flag goes on. Tool results follow the same rule as tool
# descriptions: state facts, never direct the model.
READ_ALOUD_OFFER_TEXT = (
    "Speko can also read this aloud on a phone call to your own number, "
    "or call a business for you, with sessions.phone.create."
)

Arm = Literal["offer", "holdout"]


def offer_enabled() -> bool:
    return os.environ.get(READ_ALOUD_OFFER_ENV_VAR, "").strip().lower() in {"true", "1"}


def experiment_arm(organization_id: str) -> Arm:
    """Stable 50/50 arm: parity of the first byte of sha256("<experiment>:<org>")."""
    digest = hashlib.sha256(f"{EXPERIMENT_ID}:{organization_id}".encode()).digest()
    return "offer" if digest[0] % 2 == 0 else "holdout"


def _organization_id() -> str | None:
    claims = getattr(get_access_token(), "claims", None)
    if not isinstance(claims, dict):
        return None
    value = claims.get("organization_id")
    return value if isinstance(value, str) and value else None


def _session_can_place_calls() -> bool:
    # Imported at call time: profiles imports the action manifest.
    from spekoai_mcp.profiles import session_can_call

    return session_can_call(PHONE_CALLING_TOOL)


def with_read_aloud_offer(result: ToolResult) -> ToolResult:
    """Return ``result``, with the offer line appended when this call is in the offer arm."""
    try:
        if not offer_enabled():
            return result
        execution_client = classify_execution_client(current_client_ua())["execution_client"]
        if execution_client != ELIGIBLE_CLIENT:
            return result
        organization_id = _organization_id()
        if organization_id is None or not _session_can_place_calls():
            return result
        arm = experiment_arm(organization_id)
        attribution.submit_event(
            {
                "event": "mcp_experiment_exposure",
                "event_version": 1,
                "experiment": EXPERIMENT_ID,
                "arm": arm,
                "organization_id": organization_id,
                "execution_client": execution_client,
                "tool": "audio.synthesize",
                "invocation_id": attribution.current_invocation_id(),
                "exposed_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            }
        )
        if arm == "offer":
            return _append_line(result, READ_ALOUD_OFFER_TEXT)
        return result
    except Exception:
        # A growth nudge must never fail or change a successful synthesis otherwise.
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
