"""Tests for the ChatGPT host's deployment-bound tool profile.

The profile is what OpenAI's Plugin Directory reviews, so these tests are
written as the policy checks a reviewer would run, not as a spelling test on
a list. Each anti-test names the rule it enforces.
"""

from __future__ import annotations

import json
import re

import pytest
from fastmcp.exceptions import NotFoundError, ToolError

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.profiles as profiles
from spekoai_mcp.action_tools import (
    ACTION_TOOL_NAME_BY_FUNCTION,
    ACTION_TOOL_NAMES,
    DESTRUCTIVE_ACTION_TOOL_NAMES,
    DISCLOSURE_OPENER,
    DISCLOSURE_RULE,
)
from spekoai_mcp.builder_tools import BUILDER_TOOL_NAMES
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import (
    BUILDER_PROFILE,
    BUILDER_PROFILE_TOOL_NAMES,
    CHATGPT_PROFILE,
    CHATGPT_PROFILE_TOOL_NAMES,
    CONNECTOR_PROFILE,
    DEFAULT_MANIFEST_ONLY_TOOL_NAMES,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
)
from spekoai_mcp.server import create_server

DEFAULT_TOOL_NAMES = ACTION_TOOL_NAMES + DEFAULT_MANIFEST_ONLY_TOOL_NAMES + DOCS_TOOL_NAMES

DESTRUCTIVE_PUBLIC_NAMES = {
    ACTION_TOOL_NAME_BY_FUNCTION[fn] for fn in DESTRUCTIVE_ACTION_TOOL_NAMES
}


def _force_deployment_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    """Configure the tool surface selected by one deployed host."""
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)


# --- the preset is coherent -------------------------------------------------


def test_chatgpt_preset_is_composed_of_known_tools() -> None:
    known = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    unknown = [n for n in CHATGPT_PROFILE_TOOL_NAMES if n not in known]
    assert unknown == [], f"preset names no such tool: {unknown}"


def test_chatgpt_preset_has_no_duplicates() -> None:
    assert len(CHATGPT_PROFILE_TOOL_NAMES) == len(set(CHATGPT_PROFILE_TOOL_NAMES))


def test_chatgpt_preset_borrows_only_the_two_catalogue_reads() -> None:
    """Builder-only tools are hidden everywhere else, so borrowing one is an
    explicit opt-in. Exactly two are borrowed — the voice and model catalogues,
    because a plugin that can speak in hundreds of voices should be able to say
    which ones exist. `code_snippets.get` is not: nobody pastes integration code
    in ChatGPT."""
    borrowed = set(CHATGPT_PROFILE_TOOL_NAMES) & set(BUILDER_TOOL_NAMES)
    assert borrowed == {"voices.list", "models.list"}
    assert "code_snippets.get" not in CHATGPT_PROFILE_TOOL_NAMES


def test_chatgpt_preset_keeps_the_calling_wedge() -> None:
    for kept in (
        "sessions.phone.create",
        "sessions.transcript.get",
        "sessions.recording.get",
        "agents.create",
        "agents.test_call",
    ):
        assert kept in CHATGPT_PROFILE_TOOL_NAMES, f"{kept} missing"


def test_chatgpt_preset_keeps_the_audio_tools() -> None:
    """Anthropic's directory bans generated audio and drops audio.synthesize.
    OpenAI has no such rule, so both one-shot audio tools stay."""
    assert "audio.synthesize" in CHATGPT_PROFILE_TOOL_NAMES
    assert "audio.transcribe" in CHATGPT_PROFILE_TOOL_NAMES


def test_every_referenced_tool_is_present() -> None:
    """A kept tool whose description names a dropped tool dead-ends the model
    on 'Unknown tool'. agents.create names preview_stacks; agents.test_call
    names the three review tools."""
    for referenced in (
        "agents.preview_stacks",
        "calls.get",
        "sessions.transcript.get",
        "calls.recording.get",
    ):
        assert referenced in CHATGPT_PROFILE_TOOL_NAMES, f"{referenced} referenced but dropped"


# --- OpenAI policy anti-criteria -------------------------------------------


def test_no_purchase_path_is_exposed() -> None:
    """OpenAI bans selling digital goods or initiating checkout from a plugin.
    Provisioning a phone number is a paid purchase."""
    for banned in (
        "phone_numbers.create",
        "phone_numbers.available.search",
        "phone_numbers.update",
    ):
        assert banned not in CHATGPT_PROFILE_TOOL_NAMES, f"{banned} is a purchase path"


def test_no_billing_read_is_exposed() -> None:
    """Credits and usage reads are the top of the upsell funnel; the same rule
    that bans checkout bans steering a user toward it."""
    billing = [
        n
        for n in CHATGPT_PROFILE_TOOL_NAMES
        if n.startswith("credits.") or n.startswith("usage.") or n == "organization.get"
    ]
    assert billing == [], f"billing surface leaked: {billing}"


def test_no_destructive_tool_is_exposed() -> None:
    leaked = sorted(set(CHATGPT_PROFILE_TOOL_NAMES) & DESTRUCTIVE_PUBLIC_NAMES)
    assert leaked == [], f"destructive tools leaked: {leaked}"


def test_no_bulk_or_scheduled_calling_is_exposed() -> None:
    """One explicit tool invocation per outbound call — no fan-out, no
    unattended scheduling."""
    leaked = [
        n
        for n in CHATGPT_PROFILE_TOOL_NAMES
        if n.startswith(("agents.evals.", "agents.monitors.", "agents.monitoring."))
        or n == "evals.get"
    ]
    assert leaked == [], f"bulk/scheduled tooling leaked: {leaked}"


def test_no_developer_only_tooling_is_exposed() -> None:
    """Vendor migration, agent dev-ops and document ingestion belong in the
    dashboard and the IDE, not in a consumer chat surface."""
    leaked = [
        n
        for n in CHATGPT_PROFILE_TOOL_NAMES
        if n.startswith(("migration.", "knowledge_bases.", "agents.tools."))
        or n in {"agents.update", "agents.deploy", "agents.versions.list", "share_cards.create"}
    ]
    assert leaked == [], f"developer-only tooling leaked: {leaked}"


# --- resolution -------------------------------------------------------------


def test_chatgpt_profile_resolves_from_deployment_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, "chatgpt")
    assert profiles.current_profile() == CHATGPT_PROFILE


def test_chatgpt_is_a_directory_profile() -> None:
    assert CHATGPT_PROFILE in DIRECTORY_PROFILES
    assert CONNECTOR_PROFILE in DIRECTORY_PROFILES
    assert BUILDER_PROFILE not in DIRECTORY_PROFILES


# --- listing and calling ----------------------------------------------------


async def test_chatgpt_profile_lists_exactly_the_preset_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, "chatgpt")
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == CHATGPT_PROFILE_TOOL_NAMES


async def test_out_of_profile_tools_are_uncallable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden must also mean unreachable — a reviewer who reads the source and
    calls the name anyway gets the same 'Unknown tool' an unregistered name
    would give."""
    _force_deployment_profile(monkeypatch, "chatgpt")
    mcp = create_server()
    for name in [
        "agents.delete",
        "phone_numbers.create",
        "credits.balance.get",
        "agents.evals.run",
        "share_cards.create",
    ]:
        assert name not in CHATGPT_PROFILE_TOOL_NAMES
        with pytest.raises(NotFoundError, match=f"Unknown tool: '{name}'"):
            await mcp.call_tool(name, {})


async def test_chatgpt_profile_tools_carry_review_grade_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI names missing or wrong action labels as a common cause of
    rejection, so every advertised tool must carry a title, an output schema
    and all three hints — with the reads marked read-only and the writes not."""
    _force_deployment_profile(monkeypatch, "chatgpt")
    tools = await create_server().list_tools()
    assert all(tool.title for tool in tools)
    assert all(tool.output_schema for tool in tools)
    assert all(tool.annotations is not None for tool in tools)

    by_name = {tool.name: tool for tool in tools}
    for read_only in (
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
    ):
        assert by_name[read_only].annotations.read_only_hint is True, read_only
    for write in ("agents.create", "agents.test_call", "sessions.phone.create"):
        assert by_name[write].annotations.read_only_hint is False, write
    for name, tool in by_name.items():
        assert tool.annotations.destructive_hint is False, name


# --- the other surfaces are untouched ---------------------------------------


async def test_default_profile_is_unchanged_by_the_chatgpt_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, None)
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == DEFAULT_TOOL_NAMES


async def test_builder_profile_is_unchanged_by_the_chatgpt_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, "builder")
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == BUILDER_PROFILE_TOOL_NAMES


async def test_connector_profile_is_unchanged_by_the_chatgpt_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, "connector")
    names = [tool.name for tool in await create_server().list_tools()]
    excluded = profiles.CONNECTOR_EXCLUDED_TOOL_NAMES
    prefixes = profiles.CONNECTOR_EXCLUDED_PREFIXES
    assert names == [
        n for n in DEFAULT_TOOL_NAMES if n not in excluded and not n.startswith(prefixes)
    ]


async def test_unknown_deployment_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_deployment_profile(monkeypatch, "chat-gpt")
    with pytest.raises(RuntimeError, match=DEFAULT_PROFILE_ENV_VAR):
        await create_server().list_tools()


# --- disclosure -------------------------------------------------------------


def test_disclosure_fires_on_the_chatgpt_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = action_tools.apply_directory_disclosure({"firstMessage": "Hi, this is Ava."})
    assert body["firstMessage"].startswith(DISCLOSURE_OPENER)
    assert DISCLOSURE_RULE in body["systemPrompt"]


def test_disclosure_does_not_fire_on_the_default_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: None)
    body = action_tools.apply_directory_disclosure({"firstMessage": "Hi, this is Ava."})
    assert body["firstMessage"] == "Hi, this is Ava."
    assert "systemPrompt" not in body


# --- every referenced tool is present, checked programmatically -------------


async def test_no_advertised_description_names_a_withheld_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISC-9/10 pin the two cross-references found by hand. This closes the
    class: scan all 16 descriptions for dotted tool-name-shaped tokens and
    assert every one that names a real Speko tool is itself in the profile.
    A description that points at a dropped tool dead-ends the model on
    'Unknown tool' with no way to recover.
    """
    _force_deployment_profile(monkeypatch, "chatgpt")
    tools = await create_server().list_tools()
    all_tools = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    # Descriptions name tools either dotted (sessions.transcript.get) or by the
    # underlying function name (preview_stacks, parse_external_config).
    by_function = {fn: public for fn, public in ACTION_TOOL_NAME_BY_FUNCTION.items()}

    dangling: list[tuple[str, str]] = []
    for tool in tools:
        schema = json.dumps(getattr(tool, "parameters", {}))
        text = f"{tool.description or ''} {schema}"
        for token in set(re.findall(r"[a-z_]+(?:\.[a-z_]+)+|[a-z]+_[a-z_]+", text)):
            referenced = token if token in all_tools else by_function.get(token)
            if referenced is None or referenced not in all_tools:
                continue
            if referenced not in CHATGPT_PROFILE_TOOL_NAMES:
                dangling.append((tool.name, referenced))

    # parse_external_config is the one documented exception: agents.create
    # mentions it as a migrations-only escape hatch, not a step in any ChatGPT
    # workflow, exactly as the builder preset documents.
    dangling = [d for d in dangling if d[1] != "migration.external_config.parse"]
    assert dangling == [], f"descriptions point at withheld tools: {dangling}"


# --- every advertised tool actually dispatches ------------------------------


async def test_every_advertised_tool_is_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listed must mean callable. If the middleware ever intersects where it
    should union, a tool could advertise and then fail on dispatch."""
    _force_deployment_profile(monkeypatch, "chatgpt")
    mcp = create_server()
    for name in CHATGPT_PROFILE_TOOL_NAMES:
        try:
            await mcp.call_tool(name, {})
        except NotFoundError as exc:  # the one failure this test is about
            pytest.fail(f"{name} is advertised but not dispatchable: {exc}")
        except Exception:
            # Validation errors and upstream failures are fine — the tool was
            # reached. Only "Unknown tool" means the profile is incoherent.
            pass


# --- emergency numbers ------------------------------------------------------


def test_emergency_and_short_codes_cannot_be_dialed() -> None:
    """A reviewer's sharpest probe is 'can it call 911'. It cannot: `to` must
    be E.164 with at least seven digits, so every emergency and short code is
    rejected before the request leaves the MCP server."""
    for number in ["911", "+911", "112", "+112", "999", "+999", "+1911", "+112345"]:
        with pytest.raises(ToolError):
            action_tools.validate_create_phone_session_body({"to": number, "agentId": "a"})
