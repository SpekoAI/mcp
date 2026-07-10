"""Per-request tool profiles for the hosted MCP endpoint.

App builders (v0, Lovable, Bolt, Replit, Base44, Figma Make) let users add
remote MCP servers whose tools inform the agent DURING code generation.
The full operational surface (sessions/numbers/KBs/evals/monitors/usage)
is too broad and too write-heavy for that use case, so the server supports
a curated builder preset selected per request via a query parameter:

    https://mcp.speko.ai/mcp?profile=builder

Design constraints (see platform issue #1169):

- The DEFAULT surface (no ``profile`` query param, or any unrecognized
  value) must stay byte-identical for existing clients. Builder-only
  tools are registered on the same server but hidden from the default
  view by :class:`ToolProfileMiddleware`, and the default tool ordering
  is untouched because builder-only tools are registered last.
- A separate path (e.g. ``/builder/mcp``) is deliberately NOT used: the
  OAuth resource indicator/audience is bound to ``/mcp`` (see
  ``auth.py``), and RFC 9728 protected-resource discovery is path-based,
  so a second path would need upstream Better Auth changes. A query
  param keeps one auth surface and zero auth changes.

The profile is resolved from the live HTTP request on every MCP request
(FastMCP's ``RequestContextMiddleware`` is installed in ``create_app``),
so one deployment serves both surfaces. Outside an HTTP request (stdio,
in-process tests) the default profile applies.
"""

from __future__ import annotations

from collections.abc import Sequence

import mcp.types as mt
from fastmcp.exceptions import NotFoundError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

PROFILE_QUERY_PARAM = "profile"
BUILDER_PROFILE = "builder"

# The curated builder preset, in the order clients see it. Reads first,
# the two sanctioned writes last (builder platforms default writes to
# ask-approval).
#
# Rule: every tool an INCLUDED tool's description tells the client to
# call must itself be included, or builder agents dead-end on "Unknown
# tool". That pulls in `agents.preview_stacks` (agents.create: "ALWAYS
# call preview_stacks FIRST") and the agents.test_call review path
# (`calls.get` + `sessions.transcript.get` + `calls.recording.get`).
# The one exception: agents.create's parenthetical mention of
# parse_external_config is a migrations-only escape hatch, not a step in
# any builder workflow, so it stays out.
BUILDER_PROFILE_TOOL_NAMES: list[str] = [
    "docs.search",
    "voices.list",
    "models.list",
    "agents.list",
    "agents.get",
    "agents.preview_stacks",
    "calls.get",
    "sessions.transcript.get",
    "calls.recording.get",
    "code_snippets.get",
    "agents.create",
    "agents.test_call",
]

# Tools that exist ONLY in the builder profile. These are registered on
# the shared server but must never leak into the default surface.
BUILDER_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "voices.list",
        "models.list",
        "code_snippets.get",
    }
)

_BUILDER_PROFILE_TOOL_SET = frozenset(BUILDER_PROFILE_TOOL_NAMES)


def current_profile() -> str | None:
    """Resolve the requested tool profile from the current HTTP request.

    Returns ``BUILDER_PROFILE`` only for an exact ``?profile=builder``
    match; anything else (missing param, unknown value, no HTTP request
    at all) resolves to ``None`` — the default profile — so existing
    clients cannot be affected by typos or future values.
    """
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - no-HTTP-context must NEVER select a profile,
        # whatever exception type FastMCP raises for it now or in the future.
        return None
    value = request.query_params.get(PROFILE_QUERY_PARAM)
    if value == BUILDER_PROFILE:
        return BUILDER_PROFILE
    return None


class ToolProfileMiddleware(Middleware):
    """Filter the tool surface per request based on the resolved profile.

    - default profile: hide (and refuse calls to) builder-only tools, so
      the advertised list and callable set are exactly the pre-profile
      surface.
    - builder profile: advertise exactly ``BUILDER_PROFILE_TOOL_NAMES``
      and refuse calls to anything else.

    Refusals raise the same ``NotFoundError("Unknown tool: ...")`` the
    FastMCP core raises for unregistered names, so a hidden tool is
    indistinguishable from a nonexistent one.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        if current_profile() == BUILDER_PROFILE:
            filtered = [tool for tool in tools if tool.name in _BUILDER_PROFILE_TOOL_SET]
            # Present the preset in its documented order: reads first,
            # the two sanctioned writes last.
            filtered.sort(key=lambda tool: BUILDER_PROFILE_TOOL_NAMES.index(tool.name))
            return filtered
        return [tool for tool in tools if tool.name not in BUILDER_ONLY_TOOL_NAMES]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, object],
    ) -> object:
        name = context.message.name
        if current_profile() == BUILDER_PROFILE:
            if name not in _BUILDER_PROFILE_TOOL_SET:
                raise NotFoundError(f"Unknown tool: {name!r}")
        elif name in BUILDER_ONLY_TOOL_NAMES:
            raise NotFoundError(f"Unknown tool: {name!r}")
        return await call_next(context)
