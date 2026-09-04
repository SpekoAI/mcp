"""Deployment-bound tool profiles for the hosted MCP endpoints.

App builders (v0, Lovable, Bolt, Replit, Base44, Figma Make) let users add
remote MCP servers whose tools inform the agent DURING code generation.
The full operational surface (sessions/numbers/KBs/evals/monitors/usage)
is too broad and too write-heavy for that use case, so the server supports
a curated builder preset on a dedicated host:

    https://builder-mcp.speko.ai/mcp

Two further hosts are published in third-party assistant directories:
``anthropic.speko.ai`` (Anthropic's MCP Directory) and ``chatgpt.speko.ai``
(OpenAI's Plugin Directory). Each is shaped by that directory's policy, so
they deliberately do not serve the same tool list.

A profile can also be the DEPLOYMENT default, via
``SPEKOAI_MCP_DEFAULT_PROFILE`` selects the immutable surface for one
deployment. Query parameters are deliberately ignored: a host is the policy
boundary, and callers cannot switch or widen its surface.

Design constraints (see platform issue #1169):

- On a deployment that leaves ``SPEKOAI_MCP_DEFAULT_PROFILE`` unset, the
  legacy default surface stays byte-identical for local development and
  backwards-compatible self-hosting.
- Every hosted deployment sets a known profile and serves it at bare ``/mcp``.
  The same path keeps RFC 9728 discovery and OAuth resource binding uniform;
  the hostname supplies the policy boundary.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import mcp.types as mt
from fastmcp.exceptions import NotFoundError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

from spekoai_mcp.action_manifest import action_entries, manifest_tool_names

DEFAULT_PROFILE_ENV_VAR = "SPEKOAI_MCP_DEFAULT_PROFILE"
BUILDER_PROFILE = "builder"
CONNECTOR_PROFILE = "connector"
CHATGPT_PROFILE = "chatgpt"
CUSTOMER_PROFILE = "customer"

# Every value SPEKOAI_MCP_DEFAULT_PROFILE will honour. Unknown non-empty values
# fail closed instead of silently widening a deployment.
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
#   live call         sessions.create (A), agents.test_call (A)
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
# One name came back on 2026-09-04. `sessions.phone.create` — placing a real
# outbound call to a real number with an agent the customer already deployed —
# is the reason a voice platform belongs in an assistant directory at all. A
# surface that can read transcripts of calls it cannot place is a viewer, not a
# connector. It is served with server-injected AI disclosure on every directory
# profile (`apply_directory_disclosure`), so the person who answers is told
# they are speaking to an AI before anything else is said, and it creates
# exactly one call per explicit tool call — no bulk, schedule or fan-out path
# reaches it. The rest of the cut stands: nothing on this surface generates
# speech on demand, and nothing configures or deploys an agent. See
# DIRECTORY_CALLING_TOOL_NAMES.
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
        # live session / call creation. `sessions.phone.create` is NOT here:
        # see DIRECTORY_CALLING_TOOL_NAMES.
        "sessions.create",
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
        "knowledge_bases.create",
        "knowledge_bases.documents.create",
        "knowledge_bases.documents.delete",
        "knowledge_bases.documents.finalize",
        # phone number provisioning — inbound capacity for an armed agent
        "phone_numbers.create",
        "phone_numbers.update",
        # Irreversible deletes and the one outward-facing create. These are NOT
        # "arming" — they remove capability or publish a page — so the earlier
        # cut deliberately kept them. They come off anyway, for a different
        # reason: a directory listing has to be describable in one honest
        # clause, and with these present the surface could not be called a read
        # surface. `phone_numbers.delete` also releases a billed number, and
        # `share_cards.create` publishes a public page for an agent build.
        # Reads of all four resources stay.
        "agents.delete",
        "phone_numbers.delete",
        "knowledge_bases.delete",
        "share_cards.create",
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

# Names from that enumeration we serve anyway, and why. Kept as its own
# constant so the deviation is explicit and greppable rather than an absence in
# a set, and so a test can pin the exact scope of it.
#
# `sessions.phone.create` places one outbound call, to one number, per tool
# call, using an agent the customer deployed through some other surface. It is
# the product's whole reason to exist in a directory. Disclosure is injected
# server side on this path for every profile in DIRECTORY_PROFILES, so it is
# not the undisclosed-persona case the directory team saw in review.
DIRECTORY_CALLING_TOOL_NAMES: frozenset[str] = frozenset({"sessions.phone.create"})

# What the directory surface actually withholds today: their enumeration minus
# the calling path we reinstated. Assertions about the published surface use
# this; DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES stays a verbatim record of the ask.
DIRECTORY_WITHHELD_TOOL_NAMES: frozenset[str] = (
    DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES - DIRECTORY_CALLING_TOOL_NAMES
)

# The curated ChatGPT preset, published to OpenAI's Plugin Directory as
# `https://chatgpt.speko.ai/mcp`.
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
    """Return the immutable tool profile configured for this deployment.

    Read from ``SPEKOAI_MCP_DEFAULT_PROFILE`` on every call rather than at
    import, so tests can set it without reloading the module. Unset returns
    ``None`` for backwards-compatible self-hosting and local development.

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

    A host-bound profile is the fix, because it cannot be dropped by a URL
    rewrite, a CDN rule, or a listing field somebody retyped. A deployment
    that sets ``SPEKOAI_MCP_DEFAULT_PROFILE=connector`` serves the connector
    surface at bare ``/mcp``, with no query string anywhere in the contract.

    Query parameters never participate in selection. On a restricted
    deployment the restriction is a property of the host, not a default a
    caller can opt out of. An unknown non-empty environment value raises rather
    than silently exposing the legacy surface.
    """
    value = (os.environ.get(DEFAULT_PROFILE_ENV_VAR) or "").strip()
    if not value:
        return None
    if value not in KNOWN_PROFILES:
        known = ", ".join(sorted(KNOWN_PROFILES))
        raise RuntimeError(f"{DEFAULT_PROFILE_ENV_VAR} must be one of: {known}")
    return value


def current_profile() -> str | None:
    """Return the deployment profile; request input can never override it.

    The name is kept because action relays and disclosure policy ask for the
    profile serving the current call. It is intentionally independent of the
    HTTP request: ``?profile=builder`` and every other query value are inert.
    """
    return default_profile()


class ToolProfileMiddleware(Middleware):
    """Filter the tool surface based on the deployment's configured profile.

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
