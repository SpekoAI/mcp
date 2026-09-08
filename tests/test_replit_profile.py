"""Tests for the Replit host's deployment-bound tool profile.

Replit has no review gate, so unlike the directory presets these are not policy
checks. They are the checks that stop the preset from regressing into the thing
that made Replit Agent ignore Speko in the first place: a tool list whose front
is account administration and whose build-time tools arrive last.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import NotFoundError

import spekoai_mcp.profiles as profiles
from spekoai_mcp.action_tools import (
    ACTION_TOOL_NAME_BY_FUNCTION,
    ACTION_TOOL_NAMES,
    DESTRUCTIVE_ACTION_TOOL_NAMES,
)
from spekoai_mcp.builder_tools import BUILDER_TOOL_NAMES
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import (
    BUILDER_PROFILE_TOOL_NAMES,
    DEFAULT_MANIFEST_ONLY_TOOL_NAMES,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
    KNOWN_PROFILES,
    REPLIT_PROFILE,
    REPLIT_PROFILE_TOOL_NAMES,
)
from spekoai_mcp.server import create_server

DEFAULT_TOOL_NAMES = ACTION_TOOL_NAMES + DEFAULT_MANIFEST_ONLY_TOOL_NAMES + DOCS_TOOL_NAMES

DESTRUCTIVE_PUBLIC_NAMES = {
    ACTION_TOOL_NAME_BY_FUNCTION[fn] for fn in DESTRUCTIVE_ACTION_TOOL_NAMES
}


def _force_deployment_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)


async def _served_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _force_deployment_profile(monkeypatch, REPLIT_PROFILE)
    server = create_server()
    return [tool.name for tool in await server.list_tools()]


# --- the preset is coherent -------------------------------------------------


def test_replit_is_a_known_profile() -> None:
    assert REPLIT_PROFILE in KNOWN_PROFILES


def test_replit_preset_is_composed_of_known_tools() -> None:
    known = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    unknown = [n for n in REPLIT_PROFILE_TOOL_NAMES if n not in known]
    assert unknown == [], f"preset names no such tool: {unknown}"


def test_replit_preset_has_no_duplicates() -> None:
    assert len(REPLIT_PROFILE_TOOL_NAMES) == len(set(REPLIT_PROFILE_TOOL_NAMES))


async def test_replit_serves_exactly_the_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    served = await _served_names(monkeypatch)
    assert served == REPLIT_PROFILE_TOOL_NAMES


# --- order is the point -----------------------------------------------------


async def test_code_snippets_is_the_first_tool_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool that answers "build me a page with a voice assistant" leads.

    On the builder preset it arrives last, behind two account-read tools whose
    output schemas dominate the payload. That ordering is the regression this
    test exists to prevent.
    """
    served = await _served_names(monkeypatch)
    assert served[0] == "code_snippets.get"


async def test_build_time_tools_precede_account_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    served = await _served_names(monkeypatch)
    build_time = ["code_snippets.get", "voices.list", "models.list", "docs.search"]
    account = ["agents.list", "agents.get"]
    assert max(served.index(n) for n in build_time) < min(served.index(n) for n in account)


WRITE_TOOL_NAMES = {
    "knowledge_bases.create",
    "knowledge_bases.documents.create",
    "knowledge_bases.documents.finalize",
    "phone_numbers.kyb.submit",
    "agents.create",
    "agents.update",
    "agents.deploy",
    "agents.test_call",
    "sessions.create",
    "sessions.phone.create",
}


async def test_every_write_follows_every_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Builder platforms default writes to ask-approval, so a run of approval
    prompts at the end reads better than prompts scattered through the list."""
    served = await _served_names(monkeypatch)
    reads = [n for n in served if n not in WRITE_TOOL_NAMES]
    writes = [n for n in served if n in WRITE_TOOL_NAMES]
    assert writes, "preset serves no writes"
    assert max(served.index(n) for n in reads) < min(served.index(n) for n in writes)


# --- the second question is reachable ---------------------------------------


async def test_knowledge_base_path_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """A voice FAQ needs somewhere to put the FAQ. `builder` has no KB tools."""
    served = await _served_names(monkeypatch)
    for name in (
        "knowledge_bases.list",
        "knowledge_bases.create",
        "knowledge_bases.documents.create",
        "knowledge_bases.documents.finalize",
    ):
        assert name in served
        assert name not in BUILDER_PROFILE_TOOL_NAMES


async def test_telephony_path_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Add a phone number" is the next ask after a working in-app assistant."""
    served = await _served_names(monkeypatch)
    for name in ("phone_numbers.list", "phone_numbers.kyb.get", "sessions.phone.create"):
        assert name in served
        assert name not in BUILDER_PROFILE_TOOL_NAMES


async def test_builder_only_tools_are_reachable_here(monkeypatch: pytest.MonkeyPatch) -> None:
    served = await _served_names(monkeypatch)
    for name in profiles.BUILDER_ONLY_TOOL_NAMES:
        assert name in served


# --- what stays out ---------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [
        "gateway.",
        "agents.evals.",
        "agents.monitors.",
        "agents.monitoring.",
        "billing.",
        "api_keys.",
        "scenarios.",
        "migration.",
        "agent_access.",
        "operations.",
    ],
)
async def test_operational_surfaces_are_withheld(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    served = await _served_names(monkeypatch)
    assert [n for n in served if n.startswith(prefix)] == []


async def test_no_destructive_tool_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    served = await _served_names(monkeypatch)
    assert [n for n in served if n in DESTRUCTIVE_PUBLIC_NAMES] == []


async def test_withheld_tools_are_not_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_deployment_profile(monkeypatch, REPLIT_PROFILE)
    server = create_server()
    for name in ("agents.delete", "api_keys.create", "gateway.usage.get"):
        assert name not in REPLIT_PROFILE_TOOL_NAMES
        with pytest.raises(NotFoundError, match=f"Unknown tool: '{name}'"):
            await server.call_tool(name, {})


# --- disclosure is deliberately not applied ---------------------------------


def test_replit_is_not_a_directory_profile() -> None:
    """`apply_directory_disclosure` overwrites `firstMessage`.

    On a directory surface that is the policy. On Replit it would silently
    replace a builder's own greeting on an in-app session where nobody is
    being cold-called, so this preset stays out — as `builder` does. If Replit
    ever publishes a disclosure rule, this assertion is the thing to flip.
    """
    assert REPLIT_PROFILE not in DIRECTORY_PROFILES
