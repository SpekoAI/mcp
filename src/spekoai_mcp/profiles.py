"""Per-request tool profiles for the hosted MCP endpoint.

App builders (v0, Lovable, Bolt, Replit, Base44, Figma Make) let users add
remote MCP servers whose tools inform the agent DURING code generation.
The full operational surface (sessions/numbers/KBs/evals/monitors/usage)
is too broad and too write-heavy for that use case, so the server supports
a curated builder preset selected per request via a query parameter:

    https://mcp.speko.ai/mcp?profile=builder

Two further presets are published in third-party assistant directories:
``?profile=connector`` (Anthropic's MCP Directory) and ``?profile=chatgpt``
(OpenAI's Plugin Directory). Each is shaped by that directory's policy, so
they are deliberately not the same list.

Design constraints (see platform issue #1169):

- The DEFAULT surface (no ``profile`` query param, or any unrecognized
  value) must stay byte-identical for existing clients. Builder-only
  tools are registered on the same server but hidden from the default
  view by :class:`ToolProfileMiddleware`, and the default tool ordering
  is untouched because builder-only tools are registered last.
- A separate path (e.g. ``/builder/mcp``) is deliberately NOT used. A query
  parameter keeps one API-key authentication surface.

The profile is resolved from the live HTTP request on every MCP request
(FastMCP's ``RequestContextMiddleware`` is installed in ``create_app``),
    so one deployment serves both surfaces. Outside an HTTP request the
    default profile applies.
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
CONNECTOR_PROFILE = "connector"
CHATGPT_PROFILE = "chatgpt"

# The `connector` profile is the surface published in assistant directories
# (Anthropic's MCP Directory first). It keeps the full operational surface —
# including real outbound calling via `sessions.phone.create` — and removes
# only the capabilities that read as bulk or unattended calling:
#
#   agents.evals.*     — one `evals.run` fans out into many sessions at once
#   agents.monitors.*  — scoring rules over completed runs; harmless, but the
#                        name reads as scheduled automation to a reviewer
#
# Directory policy requires that each outbound call be individually authorised
# by the user, with no bulk, scheduled, or unattended calling exposed. After
# this cut every remaining path to a call is one call per explicit tool
# invocation. Disclosure is enforced separately, in action_tools.
CONNECTOR_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "agents.evals.",
    "agents.monitors.",
)

# Tools withheld from the published directory surface for policy rather than
# capability reasons. Anthropic's Software Directory Policy prohibits
# "software that uses AI models to generate images, video, or audio content",
# so `audio.synthesize` — which returns generated speech to the client — is
# not offered there.
#
# `audio.transcribe` deliberately stays: speech-to-text generates no audio and
# returns only text, so it is outside that prohibition.
#
# Direct MCP clients (Claude Code, Codex, Cursor) keep the full surface on the
# default `/mcp` path.
CONNECTOR_EXCLUDED_TOOL_NAMES: frozenset[str] = frozenset({"audio.synthesize"})

# The curated ChatGPT preset, published to OpenAI's Plugin Directory as
# `https://mcp.speko.ai/mcp?profile=chatgpt`.
#
# This is a SEPARATE profile from `connector`, not a reuse of it, because the
# two directories forbid different things:
#
#   - Anthropic bans AI-generated audio, so `connector` drops audio.synthesize.
#     OpenAI has no such rule, so ChatGPT KEEPS it — synthesizing speech in a
#     voice and language ChatGPT cannot produce itself is half the offer.
#   - OpenAI bans selling digital goods, subscriptions, credits and tokens
#     through a plugin, and bans checkout or upgrade-initiation paths. That
#     removes phone_numbers.create / .available.search / .update (provisioning
#     a number is a paid purchase) and every credits.* and usage.* read.
#
# What is left is one consumer workflow — place a call, watch it, read what was
# said — plus the two one-shot audio tools. Everything that is account
# administration, agent dev-ops, document ingestion, vendor migration, or
# destructive is out: not because OpenAI forbids it, but because a focused tool
# set is what the review gate scores and what the model routes well over.
#
# Same referenced-tool rule as the builder preset: every tool a KEPT tool's
# description names must itself be kept. That pulls in agents.preview_stacks
# (named by agents.create as the source of stack tiers) and the
# agents.test_call review path (calls.get + sessions.transcript.get +
# calls.recording.get). The one exception is the same one: agents.create's
# mention of parse_external_config is a migrations-only escape hatch, not a
# step in any ChatGPT workflow.
#
# Reads first, writes last, in the order clients see them.
CHATGPT_PROFILE_TOOL_NAMES: list[str] = [
    "docs.search",
    "agents.list",
    "agents.get",
    "agents.preview_stacks",
    "phone_numbers.list",
    "sessions.list",
    "sessions.get",
    "sessions.transcript.get",
    "sessions.recording.get",
    "calls.get",
    "calls.recording.get",
    "audio.transcribe",
    "audio.synthesize",
    "agents.create",
    "agents.test_call",
    "sessions.phone.create",
]

_CHATGPT_PROFILE_TOOL_SET = frozenset(CHATGPT_PROFILE_TOOL_NAMES)

# Profiles published in a third-party assistant directory. Every outbound call
# created through one of these MUST disclose that the caller is an AI (see
# `apply_directory_disclosure` in action_tools) — direct MCP clients on the
# default path are not rewritten.
DIRECTORY_PROFILES: frozenset[str] = frozenset({CONNECTOR_PROFILE, CHATGPT_PROFILE})

# The curated builder preset, in the order clients see it. Reads first,
# the two sanctioned writes last (builder platforms default writes to
# ask-approval).
#
# Rule: every tool an INCLUDED tool's description REFERS to must itself be
# included, or builder agents dead-end on "Unknown tool". That pulls in
# `agents.preview_stacks` (named by agents.create as the source of stack
# tiers) and the agents.test_call review path (`calls.get` +
# `sessions.transcript.get` + `calls.recording.get`). The one exception:
# agents.create's mention of parse_external_config is a migrations-only
# escape hatch, not a step in any builder workflow, so it stays out.
#
# Descriptions state facts and never direct the model — see the directory
# policy acknowledgement on tool descriptions. Sequencing that used to live
# in descriptions is enforced by validate_create_agent_body instead, which
# returns next_step= guidance in its error.
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
    """Resolve the tool profile for the current HTTP request.

    Returns a profile name only for an exact match against one of the three
    known values; anything else (missing param, unknown value, no HTTP request
    at all) resolves to ``None`` — the default profile — so existing clients
    cannot be affected by typos or future values.

    Resolution is per request, and the invariant that makes that safe is worth
    naming because the failure mode is silent: if a client ever stopped
    repeating the query string, a request would not error, it would fall back
    to the full default surface — which on a published directory profile means
    serving exactly the tools the listing states are absent.

    Measured 2026-08-22, all three legs green:

    - the real streamable-http client repeats the whole URL on every request
      (asserted in ``test_chatgpt_profile_http.py``, not assumed);
    - this server advertises protocol ``2026-07-28`` and runs stateless on it,
      so there is no second, session-shaped path a request could arrive by;
    - ``mcp.speko.ai/mcp`` answers directly with no redirect, and the
      trailing-slash ``/mcp/`` 307 preserves the query string.

    Residual risk sits entirely outside this app: a proxy or CDN rule that
    rewrites the URL without its query. Nothing here can detect that — a
    query-less request is indistinguishable from a legitimate default-surface
    client — so it is checked at publish time instead, by confirming the
    directory's tool scan returns the preset's count and not the default's.
    """
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - no-HTTP-context must NEVER select a profile,
        # whatever exception type FastMCP raises for it now or in the future.
        return None
    value = request.query_params.get(PROFILE_QUERY_PARAM)
    if value in (BUILDER_PROFILE, CONNECTOR_PROFILE, CHATGPT_PROFILE):
        return value
    return None


class ToolProfileMiddleware(Middleware):
    """Filter the tool surface per request based on the resolved profile.

    - default profile: hide (and refuse calls to) builder-only tools, so
      the advertised list and callable set are exactly the pre-profile
      surface.
    - builder profile: advertise exactly ``BUILDER_PROFILE_TOOL_NAMES``
      and refuse calls to anything else.
    - chatgpt profile: advertise exactly ``CHATGPT_PROFILE_TOOL_NAMES``
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
        profile = current_profile()
        if profile == BUILDER_PROFILE:
            filtered = [tool for tool in tools if tool.name in _BUILDER_PROFILE_TOOL_SET]
            # Present the preset in its documented order: reads first,
            # the two sanctioned writes last.
            filtered.sort(key=lambda tool: BUILDER_PROFILE_TOOL_NAMES.index(tool.name))
            return filtered
        if profile == CHATGPT_PROFILE:
            filtered = [tool for tool in tools if tool.name in _CHATGPT_PROFILE_TOOL_SET]
            filtered.sort(key=lambda tool: CHATGPT_PROFILE_TOOL_NAMES.index(tool.name))
            return filtered
        visible = [tool for tool in tools if tool.name not in BUILDER_ONLY_TOOL_NAMES]
        if profile == CONNECTOR_PROFILE:
            return [tool for tool in visible if not _is_connector_excluded(tool.name)]
        return visible

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, object],
    ) -> object:
        name = context.message.name
        profile = current_profile()
        if profile == BUILDER_PROFILE:
            if name not in _BUILDER_PROFILE_TOOL_SET:
                raise NotFoundError(f"Unknown tool: {name!r}")
        elif profile == CHATGPT_PROFILE:
            if name not in _CHATGPT_PROFILE_TOOL_SET:
                raise NotFoundError(f"Unknown tool: {name!r}")
        elif name in BUILDER_ONLY_TOOL_NAMES:
            raise NotFoundError(f"Unknown tool: {name!r}")
        elif profile == CONNECTOR_PROFILE and _is_connector_excluded(name):
            raise NotFoundError(f"Unknown tool: {name!r}")
        return await call_next(context)


def _is_connector_excluded(name: str) -> bool:
    """True when a tool is hidden from the directory-published surface."""
    return name in CONNECTOR_EXCLUDED_TOOL_NAMES or name.startswith(CONNECTOR_EXCLUDED_PREFIXES)
