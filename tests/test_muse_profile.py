"""Tests for the Meta Muse host's deployment-bound tool profile.

Muse is a published directory, so the checks that matter are the directory
ones: the AI-disclosure policy applies, nothing destructive is served, and the
surface is exactly the preset plus the manifest actions tagged `muse`.
"""

from __future__ import annotations

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
    CHATGPT_PROFILE_TOOL_NAMES,
    DEFAULT_MANIFEST_ONLY_TOOL_NAMES,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
    KNOWN_PROFILES,
    MUSE_PROFILE,
    MUSE_PROFILE_TOOL_NAMES,
    profile_serves_tool,
)
from spekoai_mcp.server import create_server

DEFAULT_TOOL_NAMES = ACTION_TOOL_NAMES + DEFAULT_MANIFEST_ONLY_TOOL_NAMES + DOCS_TOOL_NAMES

DESTRUCTIVE_PUBLIC_NAMES = {
    ACTION_TOOL_NAME_BY_FUNCTION[fn] for fn in DESTRUCTIVE_ACTION_TOOL_NAMES
}


async def _served_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, MUSE_PROFILE)
    server = create_server()
    return [tool.name for tool in await server.list_tools()]


def test_muse_is_a_known_directory_profile() -> None:
    assert MUSE_PROFILE in KNOWN_PROFILES
    assert MUSE_PROFILE in DIRECTORY_PROFILES


def test_muse_preset_is_composed_of_known_tools() -> None:
    known = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES)
    unknown = [n for n in MUSE_PROFILE_TOOL_NAMES if n not in known]
    assert unknown == [], f"preset names no such tool: {unknown}"


def test_muse_preset_has_no_duplicates() -> None:
    assert len(MUSE_PROFILE_TOOL_NAMES) == len(set(MUSE_PROFILE_TOOL_NAMES))


def test_muse_keeps_every_chatgpt_tool() -> None:
    missing = [n for n in CHATGPT_PROFILE_TOOL_NAMES if n not in MUSE_PROFILE_TOOL_NAMES]
    assert missing == []


async def test_muse_serves_the_preset_then_tagged_manifest_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = await _served_names(monkeypatch)
    head = served[: len(MUSE_PROFILE_TOOL_NAMES)]
    assert head == MUSE_PROFILE_TOOL_NAMES
    extra = set(served[len(MUSE_PROFILE_TOOL_NAMES) :])
    assert extra <= manifest_tool_names(MUSE_PROFILE)


async def test_muse_serves_the_billing_handoffs(monkeypatch: pytest.MonkeyPatch) -> None:
    served = set(await _served_names(monkeypatch))
    assert {
        "billing.checkout.create",
        "billing.auto_topup.setup",
        "billing.auto_topup.get",
        "billing.auto_topup.update",
        "billing.auto_topup.resume",
        "credits.balance.get",
    } <= served


async def test_muse_tool_availability_matches_the_served_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = set(await _served_names(monkeypatch))
    manifest_names = {name for profile in KNOWN_PROFILES for name in manifest_tool_names(profile)}
    candidates = set(DEFAULT_TOOL_NAMES) | set(BUILDER_TOOL_NAMES) | manifest_names
    assert {name for name in candidates if profile_serves_tool(name, MUSE_PROFILE)} == served
    assert profile_serves_tool("agents.delete", MUSE_PROFILE)
    assert not profile_serves_tool("api_keys.create", MUSE_PROFILE)
    assert not profile_serves_tool("unknown.tool", MUSE_PROFILE)


async def test_muse_serves_receptionist_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    served = set(await _served_names(monkeypatch))
    assert {"receptionist.setup", "receptionist.update_facts"} <= served


async def test_muse_serves_the_receptionist_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    served = set(await _served_names(monkeypatch))
    assert {
        "receptionist.go_live",
        "receptionist.verify_forwarding",
        "receptionist.pause",
        "receptionist.resume",
        "receptionist.settings.update",
    } <= served


async def test_muse_serves_every_customer_tool_except_developer_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = set(await _served_names(monkeypatch))
    missing = [n for n in ACTION_TOOL_NAMES if n not in served]
    assert missing == [], f"muse is missing customer tools: {missing}"
    assert not any(n.startswith(("api_keys.", "gateway.")) for n in served)


async def test_muse_serves_the_deletes_owners_need_to_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = set(await _served_names(monkeypatch))
    assert DESTRUCTIVE_PUBLIC_NAMES <= served


async def test_muse_refuses_tools_outside_the_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, MUSE_PROFILE)
    server = create_server()
    with pytest.raises(NotFoundError):
        await server.call_tool("api_keys.create", {})


async def test_muse_serves_the_integration_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    served = set(await _served_names(monkeypatch))
    assert {
        "integrations.list",
        "integrations.connect",
        "agents.integration_tools.attach",
        "agents.integration_tools.detach",
    } <= served
