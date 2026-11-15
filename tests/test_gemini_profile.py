"""Tests for the Google Gemini host's deployment-bound tool profile.

Gemini is a published directory, so the AI-disclosure policy applies. Google's
Connected Apps policy also bans agent-to-agent interactions, so no tool that
builds or tests a voice agent may be served here.
"""

from __future__ import annotations

import json
import re

import pytest
from fastmcp.exceptions import NotFoundError

from spekoai_mcp.action_manifest import manifest_tool_names
from spekoai_mcp.action_tools import (
    ACTION_TOOL_NAME_BY_FUNCTION,
    ACTION_TOOL_NAMES,
    DESTRUCTIVE_ACTION_TOOL_NAMES,
)
from spekoai_mcp.builder_tools import BUILDER_TOOL_NAMES
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import (
    DEFAULT_MANIFEST_ONLY_TOOL_NAMES,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
    GEMINI_PROFILE,
    GEMINI_PROFILE_TOOL_NAMES,
    KNOWN_PROFILES,
    profile_serves_phone_calls,
    profile_serves_tool,
)
from spekoai_mcp.server import create_server

DEFAULT_TOOL_NAMES = ACTION_TOOL_NAMES + DEFAULT_MANIFEST_ONLY_TOOL_NAMES + DOCS_TOOL_NAMES

DESTRUCTIVE_PUBLIC_NAMES = {
    ACTION_TOOL_NAME_BY_FUNCTION[fn] for fn in DESTRUCTIVE_ACTION_TOOL_NAMES
}

# Tools that create, change, deploy or test a voice agent. Google bans
# agent-to-agent interactions, and a test call is one agent talking to another.
AGENT_TO_AGENT_TOOL_NAMES = {
    "agents.create",
    "agents.update",
    "agents.deploy",
    "agents.rollback",
    "agents.test_call",
    "agents.test_call.get",
    "sessions.create",
}

# Descriptions name tools dotted (sessions.transcript.get) or by function name.
TOOL_NAME_PATTERN = re.compile(r"[a-z_]+(?:\.[a-z_]+)+|[a-z]+_[a-z_]+")


async def _served(monkeypatch: pytest.MonkeyPatch) -> list:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, GEMINI_PROFILE)
    server = create_server()
    return list(await server.list_tools())


def test_gemini_is_a_known_directory_profile() -> None:
    assert GEMINI_PROFILE in KNOWN_PROFILES
    assert GEMINI_PROFILE in DIRECTORY_PROFILES


def test_gemini_preset_is_composed_of_known_tools() -> None:
    known = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    unknown = [n for n in GEMINI_PROFILE_TOOL_NAMES if n not in known]
    assert unknown == [], f"preset names no such tool: {unknown}"


def test_gemini_preset_has_no_duplicates() -> None:
    assert len(GEMINI_PROFILE_TOOL_NAMES) == len(set(GEMINI_PROFILE_TOOL_NAMES))


def test_gemini_serves_no_agent_to_agent_tool() -> None:
    leaked = sorted(n for n in AGENT_TO_AGENT_TOOL_NAMES if profile_serves_tool(n, GEMINI_PROFILE))
    assert leaked == [], f"agent-to-agent tools leaked: {leaked}"


def test_gemini_serves_no_destructive_delete() -> None:
    leaked = sorted(set(GEMINI_PROFILE_TOOL_NAMES) & DESTRUCTIVE_PUBLIC_NAMES)
    assert leaked == [], f"destructive tools leaked: {leaked}"


def test_gemini_serves_no_purchase_billing_or_secrets() -> None:
    leaked = [
        n
        for n in GEMINI_PROFILE_TOOL_NAMES
        if n.startswith(("credits.", "usage.", "billing.", "api_keys.", "gateway.", "sms."))
        or n in {"phone_numbers.create", "phone_numbers.update", "organization.get"}
    ]
    assert leaked == []


def test_gemini_serves_phone_calls_and_the_price_quote() -> None:
    assert profile_serves_phone_calls(GEMINI_PROFILE)
    assert profile_serves_tool("sessions.phone.quote", GEMINI_PROFILE)


async def test_gemini_serves_exactly_the_preset_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = [tool.name for tool in await _served(monkeypatch)]
    head = served[: len(GEMINI_PROFILE_TOOL_NAMES)]
    assert head == GEMINI_PROFILE_TOOL_NAMES
    extra = set(served[len(GEMINI_PROFILE_TOOL_NAMES) :])
    assert extra <= manifest_tool_names(GEMINI_PROFILE)


async def test_gemini_tool_availability_matches_the_served_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = {tool.name for tool in await _served(monkeypatch)}
    manifest_names = {name for profile in KNOWN_PROFILES for name in manifest_tool_names(profile)}
    candidates = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES) | manifest_names
    assert {name for name in candidates if profile_serves_tool(name, GEMINI_PROFILE)} == served


async def test_every_tool_a_served_description_names_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kept tool whose description or schema names a dropped tool dead-ends
    the model on 'Unknown tool'."""
    tools = await _served(monkeypatch)
    served = {tool.name for tool in tools}
    all_tools = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    dangling = set()
    for tool in tools:
        text = f"{tool.description or ''} {json.dumps(tool.parameters)}"
        for token in set(TOOL_NAME_PATTERN.findall(text)):
            referenced = token if token in all_tools else ACTION_TOOL_NAME_BY_FUNCTION.get(token)
            if referenced in all_tools and referenced not in served:
                dangling.add((tool.name, referenced))
    assert dangling == set(), f"descriptions name tools this host does not serve: {dangling}"


async def test_gemini_refuses_tools_outside_the_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, GEMINI_PROFILE)
    server = create_server()
    for name in ("agents.create", "agents.test_call", "api_keys.create"):
        with pytest.raises(NotFoundError):
            await server.call_tool(name, {})
