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

A profile can also be the DEPLOYMENT default, via
``SPEKOAI_MCP_DEFAULT_PROFILE``. That is how a directory-published surface is
served at bare ``/mcp`` with no query string in the contract at all — see
:func:`default_profile` for why a query parameter turned out to be the wrong
place to put a policy boundary, and Anthropic's 2026-08-27 review for the
refutation that forced it.

Design constraints (see platform issue #1169):

- On a deployment that leaves ``SPEKOAI_MCP_DEFAULT_PROFILE`` unset, the
  DEFAULT surface must stay byte-identical for existing clients. Builder-only
  tools are registered on the same server but hidden from the default
  view by :class:`ToolProfileMiddleware`, and the default tool ordering
  is untouched because builder-only tools are registered last.
- A separate path (e.g. ``/builder/mcp``) is deliberately NOT used. A query
  parameter keeps one API-key authentication surface, and the OAuth resource
  indicator is bound to ``/mcp`` (see ``auth.py``). A restricted *host* is the
  supported way to get a narrower base endpoint: same ``/mcp`` path, its own
  ``SPEKOAI_MCP_BASE_URL``, plus ``SPEKOAI_MCP_DEFAULT_PROFILE``.

The profile is resolved from the live HTTP request on every MCP request
(FastMCP's ``RequestContextMiddleware`` is installed in ``create_app``), so one
deployment serves several surfaces. Outside an HTTP request the deployment
default applies.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import mcp.types as mt
from fastmcp.exceptions import NotFoundError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

from spekoai_mcp.action_manifest import action_entries, manifest_tool_names

PROFILE_QUERY_PARAM = "profile"
DEFAULT_PROFILE_ENV_VAR = "SPEKOAI_MCP_DEFAULT_PROFILE"
BUILDER_PROFILE = "builder"
CONNECTOR_PROFILE = "connector"
CHATGPT_PROFILE = "chatgpt"
CUSTOMER_PROFILE = "customer"

# Every value `?profile=` and SPEKOAI_MCP_DEFAULT_PROFILE will honour. Anything
# else resolves to the deployment default, so a typo can never widen a surface.
KNOWN_PROFILES: frozenset[str] = frozenset(
    {BUILDER_PROFILE, CONNECTOR_PROFILE, CHATGPT_PROFILE, CUSTOMER_PROFILE}
)

_MANIFEST_TOOL_NAMES = frozenset(entry["id"] for entry in action_entries())
_DEFAULT_MANIFEST_TOOL_NAMES = manifest_tool_names("default")
_CUSTOMER_MANIFEST_TOOL_NAMES = manifest_tool_names(CUSTOMER_PROFILE)
_BUILDER_MANIFEST_TOOL_NAMES = manifest_tool_names(BUILDER_PROFILE)
_CONNECTOR_MANIFEST_TOOL_NAMES = manifest_tool_names(CONNECTOR_PROFILE)
_CHATGPT_MANIFEST_TOOL_NAMES = manifest_tool_names(CHATGPT_PROFILE)

# The `connector` profile is the surface published in assistant directories
# (Anthropic's MCP Directory first).
#
# It used to keep the full operational surface, including real outbound calling
# via `sessions.phone.create`, on the theory that Anthropic's Software Directory
# Policy bans only *generated audio* — so dropping `audio.synthesize` was
# enough and the outbound-calling wedge survived. The MCP Directory team
# refuted that on 2026-08-27:
#
#   "in this product, configuring is arming. A deployed agent speaks on
#    inbound traffic with no further tool call, so those tools produce
#    synthetic speech just as directly as the generation tool does."
#
# So the line is not "does this tool return audio" but:
#
#   **No tool on a directory surface may produce synthetic speech, or arm
#     something that will.**
#
# Four groups fall on the wrong side of that line. The nine names the
# directory team enumerated are marked (A); the rest are the same category
# reached by their own reasoning, cut now rather than in another round trip.
#
#   generation        audio.synthesize (A)
#   live call         sessions.create (A), sessions.phone.create (A),
#                     agents.test_call (A)
#   arming            agents.create (A), agents.update (A), agents.deploy (A),
#                     agents.rollback — redeploys a prior version, so it arms
#                     agents.tools.* writes — rewire what a live agent does
#                     knowledge_bases.* writes — change what a live agent says
#   inbound capacity  phone_numbers.create (A), phone_numbers.update (A)
#
# Held from the earlier cut, for bulk/unattended rather than speech reasons:
#
#   agents.evals.*    — one `evals.run` fans out into many sessions at once
#   agents.monitors.* — scoring rules over completed runs; harmless, but the
#                       name reads as scheduled automation to a reviewer
#
# What deliberately STAYS, because the team named it explicitly:
#
#   audio.transcribe  — speech-to-text generates no audio and returns only
#                       text, so it is outside the prohibition.
#   every read        — listing agents, sessions, transcripts, recordings,
#                       phone numbers, credits and usage.
#
# Also staying, and worth naming because they are writes: `agents.delete`,
# `phone_numbers.delete` and `knowledge_bases.delete` REMOVE capability rather
# than arm it, and `share_cards.create` and the `migration.*` tools produce
# drafts and documents, never speech.
#
# Direct MCP clients (Claude Code, Codex, Cursor) keep the full surface on a
# deployment whose `SPEKOAI_MCP_DEFAULT_PROFILE` is unset. Disclosure is
# enforced separately, in action_tools.
CONNECTOR_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "agents.evals.",
    "agents.monitors.",
)

# Tools withheld from the published directory surface by exact name. See the
# four groups above; every entry here either produces synthetic speech or arms
# something that will.
CONNECTOR_EXCLUDED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # generation
        "audio.synthesize",
        # live session / call creation
        "sessions.create",
        "sessions.phone.create",
        "agents.test_call",
        # agent configuration and deployment — configuring is arming
        "agents.create",
        "agents.update",
        "agents.deploy",
        "agents.rollback",
        # rewiring the tools a live agent can call is configuring it.
        # `agents.tools.list` / `.get` are reads and stay.
        "agents.tools.create",
        "agents.tools.update",
        "agents.tools.delete",
        # knowledge-base writes change what a live agent says.
        # `knowledge_bases.delete` stays: it removes capability.
        "knowledge_bases.create",
        "knowledge_bases.documents.create",
        "knowledge_bases.documents.delete",
        "knowledge_bases.documents.finalize",
        # phone number provisioning — inbound capacity for an armed agent
        "phone_numbers.create",
        "phone_numbers.update",
    }
)

# The nine names the MCP Directory team enumerated on 2026-08-27. Kept as its
# own constant so a test can assert the reply we send them is true, rather than
# re-deriving the list from the exclusion set it is meant to check.
DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "audio.synthesize",
        "sessions.create",
        "sessions.phone.create",
        "agents.test_call",
        "agents.create",
        "agents.update",
        "agents.deploy",
        "phone_numbers.create",
        "phone_numbers.update",
    }
)

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
# Two builder-only tools are borrowed. `audio.synthesize` routes by intent and
# will pick a voice on its own, so the profile is not dead-ended without them —
# but a plugin that can speak in hundreds of voices and a hundred-odd languages
# and cannot tell you which ones exist is answering half the question. Speaking
# is the differentiator here (ChatGPT cannot produce these voices itself), so
# `voices.list` and `models.list` come with it. They are pure reads.
#
# Reads first, writes last, in the order clients see them.
CHATGPT_PROFILE_TOOL_NAMES: list[str] = [
    "docs.search",
    "voices.list",
    "models.list",
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

_CHATGPT_PROFILE_TOOL_SET = frozenset(CHATGPT_PROFILE_TOOL_NAMES) | _CHATGPT_MANIFEST_TOOL_NAMES

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

_BUILDER_PROFILE_TOOL_SET = frozenset(BUILDER_PROFILE_TOOL_NAMES) | _BUILDER_MANIFEST_TOOL_NAMES


def default_profile() -> str | None:
    """The profile a request with no ``?profile=`` gets on this deployment.

    Read from ``SPEKOAI_MCP_DEFAULT_PROFILE`` on every call rather than at
    import, so a test can set it without reloading the module. Unset — the
    normal case — returns ``None``, the full default surface.

    This exists because a query parameter turned out to be the wrong place to
    put a policy boundary. The measurement in :func:`current_profile` was
    right about the transport: a real streamable-http client does repeat the
    whole URL on every request. What it could not cover is the directory
    *record*. Anthropic's MCP Directory listed us as
    ``https://mcp.speko.ai/mcp`` and scanned that, so ``?profile=connector``
    was never in play, and their review found the full surface behind a
    listing that said otherwise (2026-08-27):

        "The restricted profile does not resolve this, because the directory
         listing points at the base endpoint … The tools below need to come
         off that surface itself."

    A base-endpoint default is the fix, because it cannot be dropped by a URL
    rewrite, a CDN rule, or a listing field somebody retyped. A deployment
    that sets ``SPEKOAI_MCP_DEFAULT_PROFILE=connector`` serves the connector
    surface at bare ``/mcp``, with no query string anywhere in the contract.

    There is deliberately NO ``?profile=full`` escape hatch. On a restricted
    deployment the restriction has to be a property of the host, not a default
    a caller can opt out of — otherwise it is the same unenforceable boundary
    in a new costume. The full surface lives on a deployment that leaves this
    variable unset.
    """
    value = (os.environ.get(DEFAULT_PROFILE_ENV_VAR) or "").strip()
    return value if value in KNOWN_PROFILES else None


def current_profile() -> str | None:
    """Resolve the tool profile for the current HTTP request.

    Returns a profile name for an exact ``?profile=`` match against one of the
    known values; anything else (missing param, unknown value, no HTTP request
    at all) falls back to :func:`default_profile` for this deployment, so a
    typo or a future value can never widen the surface.

    Resolution is per request. Measured 2026-08-22, all three legs green:

    - the real streamable-http client repeats the whole URL on every request
      (asserted in ``test_chatgpt_profile_http.py``, not assumed);
    - this server advertises protocol ``2026-07-28`` and runs stateless on it,
      so there is no second, session-shaped path a request could arrive by;
    - ``mcp.speko.ai/mcp`` answers directly with no redirect, and the
      trailing-slash ``/mcp/`` 307 preserves the query string.

    The residual risk that measurement could not reach — a proxy, CDN rule or
    directory record that drops the query — is now closed by
    :func:`default_profile` instead of documented, for any deployment that
    sets it. A query-less request on a restricted host resolves to the
    restricted profile, not the full one.
    """
    try:
        request = get_http_request()
    except Exception:  # noqa: BLE001 - no-HTTP-context must NEVER widen the surface,
        # whatever exception type FastMCP raises for it now or in the future.
        return default_profile()
    value = request.query_params.get(PROFILE_QUERY_PARAM)
    if value in KNOWN_PROFILES:
        return value
    return default_profile()


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
        if profile == CUSTOMER_PROFILE:
            return [
                tool
                for tool in tools
                if tool.name not in _MANIFEST_TOOL_NAMES
                or tool.name in _CUSTOMER_MANIFEST_TOOL_NAMES
            ]
        if profile == BUILDER_PROFILE:
            filtered = [tool for tool in tools if tool.name in _BUILDER_PROFILE_TOOL_SET]
            # Present the preset in its documented order: reads first,
            # the two sanctioned writes last.
            filtered.sort(
                key=lambda tool: (
                    0,
                    BUILDER_PROFILE_TOOL_NAMES.index(tool.name),
                )
                if tool.name in BUILDER_PROFILE_TOOL_NAMES
                else (1, tool.name)
            )
            return filtered
        if profile == CHATGPT_PROFILE:
            filtered = [tool for tool in tools if tool.name in _CHATGPT_PROFILE_TOOL_SET]
            filtered.sort(
                key=lambda tool: (
                    0,
                    CHATGPT_PROFILE_TOOL_NAMES.index(tool.name),
                )
                if tool.name in CHATGPT_PROFILE_TOOL_NAMES
                else (1, tool.name)
            )
            return filtered
        visible = [
            tool
            for tool in tools
            if tool.name not in BUILDER_ONLY_TOOL_NAMES
            and (
                tool.name not in _MANIFEST_TOOL_NAMES
                or tool.name in _DEFAULT_MANIFEST_TOOL_NAMES
            )
        ]
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
        if profile == CUSTOMER_PROFILE:
            if name in _MANIFEST_TOOL_NAMES and name not in _CUSTOMER_MANIFEST_TOOL_NAMES:
                raise NotFoundError(f"Unknown tool: {name!r}")
        elif profile == BUILDER_PROFILE:
            if name not in _BUILDER_PROFILE_TOOL_SET:
                raise NotFoundError(f"Unknown tool: {name!r}")
        elif profile == CHATGPT_PROFILE:
            if name not in _CHATGPT_PROFILE_TOOL_SET:
                raise NotFoundError(f"Unknown tool: {name!r}")
        elif name in BUILDER_ONLY_TOOL_NAMES or (
            name in _MANIFEST_TOOL_NAMES and name not in _DEFAULT_MANIFEST_TOOL_NAMES
        ):
            raise NotFoundError(f"Unknown tool: {name!r}")
        elif profile == CONNECTOR_PROFILE and (
            _is_connector_excluded(name)
            or (name in _MANIFEST_TOOL_NAMES and name not in _CONNECTOR_MANIFEST_TOOL_NAMES)
        ):
            raise NotFoundError(f"Unknown tool: {name!r}")
        return await call_next(context)


def _is_connector_excluded(name: str) -> bool:
    """True when a tool is hidden from the directory-published surface."""
    return name in CONNECTOR_EXCLUDED_TOOL_NAMES or name.startswith(CONNECTOR_EXCLUDED_PREFIXES)
