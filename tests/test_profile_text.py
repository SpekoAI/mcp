"""Text, resources and prompts follow what each deployment profile serves.

OpenAI's plugin review (2026-11) found the ChatGPT host describing tools it
does not serve, advertising prices and top-ups, and listing a payment card and
two developer prompts. These tests hold every profile to the same rules: a
description, the server instructions, a resource or a prompt names a tool only
where the profile serves it.
"""

from __future__ import annotations

import json
import re

import pytest
from fastmcp.exceptions import NotFoundError

from spekoai_mcp.action_tools import ACTION_TOOL_NAME_BY_FUNCTION
from spekoai_mcp.apps import AUDIO_PLAYER_URI, TOP_UP_URI
from spekoai_mcp.builder_tools import BUILDER_TOOL_NAME_BY_FUNCTION
from spekoai_mcp.profiles import (
    CHATGPT_PROFILE,
    DEFAULT_PROFILE_ENV_VAR,
    KNOWN_PROFILES,
    profile_serves_tool,
)
from spekoai_mcp.server import create_server, instructions_for

PROFILES = [None, *sorted(KNOWN_PROFILES)]
DOTTED = re.compile(r"[a-z_]+(?:\.[a-z_]+)+")

# Manifest descriptions this package cannot edit (they are generated from
# packages/action-contracts). Each names a tool some profiles do not serve.
MANIFEST_REFERENCE_ALLOWLIST: dict[tuple[str, str], str] = {
    # Replit serves sessions.end without sessions.get.
    ("sessions.end", "sessions.get"): "manifest text; Replit only",
}


def _use(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)


async def _all_tool_names() -> set[str]:
    return {tool.name for tool in await create_server().list_tools(run_middleware=False)}


def _descriptions(schema: object) -> list[str]:
    """Every `description` string in a JSON schema, at any depth."""
    found: list[str] = []
    if isinstance(schema, dict):
        if isinstance(schema.get("description"), str):
            found.append(schema["description"])
        for value in schema.values():
            found.extend(_descriptions(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_descriptions(value))
    return found


async def _served_text(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> dict[str, str]:
    _use(monkeypatch, profile)
    return {
        tool.name: " ".join([tool.description or "", *_descriptions(tool.parameters)])
        for tool in await create_server().list_tools()
    }


# --- server instructions -----------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
async def test_instructions_name_only_served_tools(profile: str | None) -> None:
    known = await _all_tool_names()
    named = {token for token in DOTTED.findall(instructions_for(profile)) if token in known}
    unserved = sorted(name for name in named if not profile_serves_tool(name, profile))
    assert unserved == []


def test_default_and_customer_instructions_keep_their_scope() -> None:
    for profile in (None, "customer"):
        text = instructions_for(profile)
        assert "manage and deploy agents, phone numbers and knowledge bases" in text
        assert "run evals, and build or migrate SessionConfig drafts" in text
        assert "Authorization: Bearer sk_*" in text
    assert "phone_numbers.sip_connections.create then phone_numbers.import" in (
        instructions_for("customer")
    )
    assert "Import number > Add carrier connection" in instructions_for(None)


def test_chatgpt_instructions_describe_the_chatgpt_surface() -> None:
    text = instructions_for(CHATGPT_PROFILE)
    assert (
        "Use these tools to create and edit agents, test agents with simulated calls, place "
        "phone calls, read transcripts and recordings, transcribe audio, synthesize speech, "
        "read phone numbers, look up voices and models, and open and follow support tickets."
    ) in text
    for absent in ("deploy", "knowledge bases", "evals", "SessionConfig", "sk_*", "SIP"):
        assert absent not in text, absent


# --- tool descriptions -------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
async def test_descriptions_name_only_served_tools(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    known = await _all_tool_names()
    dangling = sorted(
        (tool, token)
        for tool, text in (await _served_text(monkeypatch, profile)).items()
        for token in set(DOTTED.findall(text))
        if token in known
        and not profile_serves_tool(token, profile)
        and (tool, token) not in MANIFEST_REFERENCE_ALLOWLIST
    )
    assert dangling == []


@pytest.mark.parametrize("profile", PROFILES)
async def test_descriptions_use_public_tool_names(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    """`create_agent` or `get_call` is a Python function name no client can call."""
    legacy = {**ACTION_TOOL_NAME_BY_FUNCTION, **BUILDER_TOOL_NAME_BY_FUNCTION}
    found = sorted(
        (tool, name)
        for tool, text in (await _served_text(monkeypatch, profile)).items()
        for name in legacy
        if re.search(rf"(?<![\w.]){name}(?![\w.])", text)
    )
    assert found == []


async def test_chatgpt_descriptions_name_no_price_or_top_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin guidelines 1.3 and 5.1: no pricing, top-ups or upgrades."""
    leaks = sorted(
        (tool, pattern)
        for tool, text in (await _served_text(monkeypatch, CHATGPT_PROFILE)).items()
        for pattern in (r"\$\d", r"(?i)top[- ]?up", r"(?i)upgrade", r"(?i)free credit")
        if re.search(pattern, text)
    )
    assert leaks == []


async def test_default_phone_description_keeps_its_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (await _served_text(monkeypatch, None))["sessions.phone.create"]
    assert "Calls cost $0.09/min plus telephony." in text
    assert "savedHandoffNumber" in text


async def test_chatgpt_update_agent_body_omits_the_refused_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = (await _served_text(monkeypatch, None))["agents.update"]
    chatgpt = (await _served_text(monkeypatch, CHATGPT_PROFILE))["agents.update"]
    assert "webhooks (" in default
    assert "onHuman?" in default
    assert "webhooks (" not in chatgpt
    assert "onHuman" not in chatgpt


# --- resources and prompts ---------------------------------------------------


async def test_chatgpt_lists_no_payment_card_and_no_developer_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, CHATGPT_PROFILE)
    mcp = create_server()
    uris = {str(resource.uri) for resource in await mcp.list_resources()}
    assert TOP_UP_URI not in uris
    assert AUDIO_PLAYER_URI in uris
    assert "spekoai://docs/index" in uris
    assert [prompt.name for prompt in await mcp.list_prompts()] == []

    with pytest.raises(NotFoundError, match="Unknown resource"):
        await mcp.read_resource(TOP_UP_URI)
    with pytest.raises(NotFoundError, match="Unknown prompt"):
        await mcp.render_prompt("migrate_voice_agent", {"from_platform": "vapi"})
    with pytest.raises(NotFoundError, match="Unknown prompt"):
        await mcp.render_prompt("scaffold_project", {"scenario": "quickstart"})


async def test_default_resources_and_prompts_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use(monkeypatch, None)
    mcp = create_server()
    uris = {str(resource.uri) for resource in await mcp.list_resources()}
    assert uris == {"spekoai://docs/index", AUDIO_PLAYER_URI, TOP_UP_URI}
    assert {p.name for p in await mcp.list_prompts()} == {
        "scaffold_project",
        "migrate_voice_agent",
    }
    assert (await mcp.read_resource(TOP_UP_URI)).contents


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_listed_prompt_names_only_served_tools(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    known = await _all_tool_names()
    _use(monkeypatch, profile)
    mcp = create_server()
    for prompt in await mcp.list_prompts():
        arguments = (
            {"from_platform": "vapi", "config_path": "agent.json"}
            if prompt.name == "migrate_voice_agent"
            else {"scenario": "quickstart"}
        )
        rendered = await mcp.render_prompt(prompt.name, arguments)
        text = json.dumps([message.content.text for message in rendered.messages])
        unserved = sorted(
            token
            for token in set(DOTTED.findall(text))
            if token in known and not profile_serves_tool(token, profile)
        )
        assert unserved == [], prompt.name
