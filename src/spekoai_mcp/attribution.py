"""Per-tool origin evidence. This metadata never participates in authorization."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import IPv6Address
from os import write as _write_stderr
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import mcp.types as mt
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from spekoai_mcp.attribution_export import BoundedLogExporter
from spekoai_mcp.client_classification import classify_execution_client

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
Receiver = Literal["platform", "router_direct"]
_CURRENT_HOST: ContextVar[str | None] = ContextVar("speko_mcp_host", default=None)


def _write_event(event: dict) -> None:
    """Use the existing stderr transport without shared Python logging locks.

    Direct writes avoid root-handler/TextIO locks used by HTTPX and other request
    logging. Only the daemon worker can wait on stderr. Cloud Run accepts this
    single JSON line directly; local export counts do not imply durable delivery.
    """
    message = (json.dumps(event, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    if len(message) > 4096:
        raise ValueError("Attribution log record exceeds the write limit")
    if _write_stderr(2, message) != len(message):
        raise OSError("Incomplete attribution log write")


_EXPORTER = BoundedLogExporter(_write_event)
atexit.register(_EXPORTER.close)


@dataclass
class _Invocation:
    invocation_id: str = field(default_factory=lambda: str(uuid4()))
    receivers: set[Receiver] = field(default_factory=set)
    active: bool = True


_CURRENT_INVOCATION: ContextVar[_Invocation | None] = ContextVar(
    "speko_mcp_invocation", default=None
)


def observed_host(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Accept one syntactically valid Host; ignore forwarded/client attribution headers.

    A valid host is observed transport evidence, never proof of a trusted host.
    Invalid or oversized values stay unknown instead of becoming truncated identities.
    """
    values = [value for name, value in headers if name.lower() == b"host"]
    if len(values) != 1:
        return None
    try:
        value = values[0].decode("ascii").lower()
        if not value or len(value) > 200 or re.search(r"[^a-z0-9.:[\]-]", value):
            return None
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        if not host or parsed.username or parsed.password or parsed.path:
            return None
        if ":" in host:
            if not re.fullmatch(r"\[[0-9a-f:.]+\](?::[0-9]+)?", value):
                return None
            IPv6Address(host)
        elif not all(_HOST_LABEL.fullmatch(label) for label in host.removesuffix(".").split(".")):
            return None
        if value.endswith(":") or (parsed.port is not None and not 1 <= parsed.port <= 65535):
            return None
        return value
    except (UnicodeDecodeError, ValueError):
        return None


def set_current_host(host: str | None) -> Token[str | None]:
    return _CURRENT_HOST.set(host)


def reset_current_host(token: Token[str | None]) -> None:
    _CURRENT_HOST.reset(token)


def invocation_headers() -> dict[str, str]:
    """Return immutable origin identifiers while a tool invocation is active."""
    headers: dict[str, str] = {}
    invocation = _CURRENT_INVOCATION.get()
    if invocation is not None and invocation.active:
        headers["X-Speko-Invocation-Id"] = invocation.invocation_id
    host = _CURRENT_HOST.get()
    if host is not None:
        headers["X-Speko-MCP-Host"] = host
    return headers


def mark_receiver(receiver: Receiver) -> None:
    """Record an attempted outbound request, including failures and fan-out."""
    invocation = _CURRENT_INVOCATION.get()
    if invocation is not None and invocation.active:
        invocation.receivers.add(receiver)


def _identifier(value: object) -> str | None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        return None
    # Never turn a misplaced credential into analytics identity.
    if value.startswith(("sk_", "sk-", "eyJ")):
        return None
    return value


def _principal() -> dict[str, str | None]:
    """Read only allowlisted identifiers from FastMCP's verified principal."""
    token = get_access_token()
    claims = getattr(token, "claims", None)
    if not isinstance(claims, dict):
        claims = {}
    is_api_key = claims.get("auth_method") == "api_key"
    client_id = _identifier(getattr(token, "client_id", None))
    return {
        "auth_kind": "api_key" if is_api_key else "oauth_user" if token else "anonymous",
        "client_id": client_id,
        "oauth_client_id": None if is_api_key else client_id,
        "api_key_id": _identifier(claims.get("api_key_id")) if is_api_key else None,
        "organization_id": _identifier(claims.get("organization_id")),
        "user_id": None
        if is_api_key
        else _identifier(getattr(token, "subject", None) or claims.get("sub")),
    }


class InvocationAttributionMiddleware(Middleware):
    """Observe tool completion without analytics I/O or changes to tool responses.

    Register before profile enforcement so refused and missing tools also count.
    List/resource/protocol requests are not tool invocations and never mint IDs.
    """

    def __init__(self, *, profile: str | None = None) -> None:
        self.profile = profile

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        from spekoai_mcp.http_client import current_client_ua
        from spekoai_mcp.profiles import current_profile

        invocation = _Invocation()
        token = _CURRENT_INVOCATION.set(invocation)
        started = time.monotonic()
        outcome = "failed"
        try:
            result = await call_next(context)
            outcome = "failed" if result.is_error else "succeeded"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            # Children inherit the invocation object during fan-out. Closing it
            # also prevents detached tasks from reusing an already completed ID.
            invocation.active = False
            _CURRENT_INVOCATION.reset(token)
            if os.environ.get("ATTRIBUTION_ENABLED", "").lower() in {"true", "1"}:
                try:
                    event = {
                        "event": "mcp_operation_completed",
                        "event_version": 3,
                        "completed_at": datetime.now(timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z"),
                        "invocation_id": invocation.invocation_id,
                        "action_id": _identifier(context.message.name),
                        "tool": _identifier(context.message.name),
                        "profile": self.profile or current_profile() or "default",
                        "host": _CURRENT_HOST.get(),
                        **classify_execution_client(current_client_ua()),
                        "receivers": sorted(invocation.receivers) or ["local"],
                        "outcome": outcome,
                        "duration_ms": round((time.monotonic() - started) * 1000, 3),
                        **_principal(),
                    }
                    # Bound the queue and isolate stalled logging from tool completion.
                    # Only copied metadata enters it, never request context or raw headers.
                    _EXPORTER.submit(event)
                except Exception:
                    # An unavailable exporter must never change a tool result.
                    pass
