"""Directory-created agents run speech-native on GPT-Live.

Temporary operator pin (2026-09-15). These tests are written so that removing
the pin is a visible, deliberate deletion rather than a silent drift: each one
names the rule it holds.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import (
    DIRECTORY_S2S_LANGUAGES,
    DIRECTORY_S2S_PIN,
    INCOMPATIBLE_RUNTIME_CODE,
    apply_directory_s2s_pin,
)
from spekoai_mcp.profiles import (
    CHATGPT_PROFILE,
    CONNECTOR_EXCLUDED_TOOL_NAMES,
    CONNECTOR_PROFILE,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_PROFILES,
)
from spekoai_mcp.server import create_server


@pytest.fixture
def agents_api_mock(monkeypatch: pytest.MonkeyPatch):
    """Capture what the relay actually sends to POST /v1/agents."""
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content.decode("utf-8") or "{}"),
            }
        )
        return httpx.Response(200, json={"id": "agent_1", "name": "Front Desk"})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        http_client._TEST_TRANSPORT = None


def _base_body() -> dict[str, object]:
    return {
        "name": "Front Desk",
        "systemPrompt": "You are Ava from Northside Clinic.",
        "intent": {"language": "en"},
    }


@pytest.mark.parametrize("profile", sorted(DIRECTORY_PROFILES))
def test_pin_applies_on_every_directory_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: profile)
    body = apply_directory_s2s_pin(_base_body())
    assert body["runMode"] == "s2s"
    assert body["stackPreferences"]["allowedProviders"]["s2s"] == [DIRECTORY_S2S_PIN]


def _body_in(language: str) -> dict[str, object]:
    body = _base_body()
    body["intent"] = {"language": language}
    return body


@pytest.mark.parametrize("language", ["hi", "es", "de", "ja", "zh", "ar"])
def test_language_the_s2s_catalog_cannot_serve_keeps_the_cascade(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    """The pin chooses between two WORKING stacks, never a stack with no leg.

    `GET /v1/models` -> `languages.s2s` is `en fil nb`. Pinning a Hindi agent
    to GPT-Live leaves it with no s2s leg and no STT/LLM/TTS knobs to repair
    it, because a speech-native agent has no cascade stack to configure.
    """
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = apply_directory_s2s_pin(_body_in(language))
    assert "runMode" not in body, f"{language} has no s2s leg; it must stay cascade"
    assert "stackPreferences" not in body


@pytest.mark.parametrize("language", ["en", "fil", "nb", "en-GB", "nb-NO", "EN"])
def test_language_the_s2s_catalog_serves_still_gets_the_pin(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    """Matched on the primary subtag, case-insensitively: `en-GB` is `en`."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = apply_directory_s2s_pin(_body_in(language))
    assert body["runMode"] == "s2s"
    assert body["stackPreferences"]["allowedProviders"]["s2s"] == [DIRECTORY_S2S_PIN]


def test_a_body_with_no_language_keeps_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform defaults an absent language to English, which s2s serves."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = _base_body()
    del body["intent"]
    assert apply_directory_s2s_pin(body)["runMode"] == "s2s"


def test_the_served_language_set_is_the_catalog_s2s_list() -> None:
    """Sourced from `GET /v1/models` -> `languages.s2s`, measured 2026-09-16."""
    assert DIRECTORY_S2S_LANGUAGES == frozenset({"en", "fil", "nb"})


def test_pin_is_the_one_phone_hostable_model() -> None:
    """A SIP leg can host GPT-Live and nothing else (services/phone-s2s.ts)."""
    assert DIRECTORY_S2S_PIN == "openai:gpt-live-1"


def test_pin_replaces_a_caller_supplied_s2s_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """This is a pin, not a default: a provider-direct realtime model the phone
    path cannot host must not survive it."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = _base_body()
    body["stackPreferences"] = {"allowedProviders": {"s2s": ["openai:gpt-realtime-2.1"]}}
    assert apply_directory_s2s_pin(body)["stackPreferences"]["allowedProviders"]["s2s"] == [
        DIRECTORY_S2S_PIN
    ]


def test_pin_keeps_cascade_pins_and_other_preferences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the s2s slot is ours; the rest of the row is the caller's."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = _base_body()
    body["stackPreferences"] = {
        "allowedProviders": {"stt": ["deepgram:nova-3"], "tts": ["cartesia:sonic-3.5"]},
        "someOtherPreference": True,
    }
    prefs = apply_directory_s2s_pin(body)["stackPreferences"]
    assert prefs["allowedProviders"]["stt"] == ["deepgram:nova-3"]
    assert prefs["allowedProviders"]["tts"] == ["cartesia:sonic-3.5"]
    assert prefs["someOtherPreference"] is True


def test_pin_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    once = apply_directory_s2s_pin(_base_body())
    twice = apply_directory_s2s_pin(dict(once))
    assert twice == once
    assert twice["stackPreferences"]["allowedProviders"]["s2s"] == [DIRECTORY_S2S_PIN]


def test_pipecat_bodies_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /v1/agents rejects pipecat + s2s outright, so pinning one would
    turn a valid create into a 4xx."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = _base_body()
    body["runtime"] = "pipecat"
    assert apply_directory_s2s_pin(body) == body
    assert "runMode" not in body


@pytest.mark.parametrize("profile", [None, "builder", "customer", "replit"])
def test_non_directory_surfaces_are_never_rewritten(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    """Claude Code, Codex, Cursor, the builder hosts and the customer host keep
    the routed cascade."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: profile)
    body = _base_body()
    assert apply_directory_s2s_pin(body) == _base_body()


def test_pin_is_inert_without_a_configured_profile() -> None:
    """stdio and in-process callers resolve to no profile."""
    assert apply_directory_s2s_pin(_base_body()) == _base_body()


def test_connector_surface_cannot_reach_the_pin() -> None:
    """Scope note kept honest: `agents.create` is withheld from the Anthropic
    directory surface, so the pin can only be set through `chatgpt`. A change
    that reinstates the tool there should make this test fail and be read."""
    assert "agents.create" in CONNECTOR_EXCLUDED_TOOL_NAMES


async def test_create_agent_sends_the_pin_to_the_api(
    monkeypatch: pytest.MonkeyPatch,
    agents_api_mock: list[dict[str, object]],
) -> None:
    """End to end: what the relay PUTS on the wire, not just what the helper
    returns."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CHATGPT_PROFILE)
    await create_server().call_tool("agents.create", {"body": _base_body()})

    assert len(agents_api_mock) == 1
    sent = agents_api_mock[0]["body"]
    assert sent["runMode"] == "s2s"
    assert sent["stackPreferences"]["allowedProviders"]["s2s"] == [DIRECTORY_S2S_PIN]


async def test_create_agent_on_the_default_surface_keeps_cascade(
    monkeypatch: pytest.MonkeyPatch,
    agents_api_mock: list[dict[str, object]],
) -> None:
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    await create_server().call_tool("agents.create", {"body": _base_body()})

    assert len(agents_api_mock) == 1
    sent = agents_api_mock[0]["body"]
    assert "runMode" not in sent
    assert "stackPreferences" not in sent


def test_connector_profile_name_is_still_a_directory_profile() -> None:
    assert CONNECTOR_PROFILE in DIRECTORY_PROFILES


# --- the managed-runtime hole ------------------------------------------------
#
# `runtime` is resolved org-side from a feature flag and overwrites the body, so
# an omitted `runtime` cannot tell the relay whether this org can host GPT-Live.
# The pin must not cost a create that was valid without it.


@pytest.fixture
def pipecat_org_api_mock(monkeypatch: pytest.MonkeyPatch):
    """An org whose managed runtime refuses `runMode: 's2s'`."""
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}")
        calls.append({"path": request.url.path, "body": body})
        if body.get("runMode") == "s2s":
            return httpx.Response(
                400,
                json={
                    "error": "Speech-to-speech is unavailable on the Pipecat managed runtime.",
                    "code": INCOMPATIBLE_RUNTIME_CODE,
                },
            )
        return httpx.Response(200, json={"id": "agent_1", "name": "Front Desk"})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        http_client._TEST_TRANSPORT = None


async def test_pipecat_runtime_org_still_gets_its_agent(
    monkeypatch: pytest.MonkeyPatch,
    pipecat_org_api_mock: list[dict[str, object]],
) -> None:
    """The create succeeds on the cascade stack instead of failing on our pin."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CHATGPT_PROFILE)
    out = await create_server().call_tool("agents.create", {"body": _base_body()})

    assert len(pipecat_org_api_mock) == 2, "expected one pinned attempt and one fallback"
    first, second = (call["body"] for call in pipecat_org_api_mock)
    assert first["runMode"] == "s2s"
    assert "runMode" not in second
    assert "stackPreferences" not in second
    assert out.structured_content["id"] == "agent_1"


@pytest.fixture
def rejecting_api_mock(monkeypatch: pytest.MonkeyPatch):
    """Any other 400 — the pin is not implicated and must not be retried away."""
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"path": request.url.path})
        return httpx.Response(
            400,
            json={
                "error": "systemPrompt is not a valid prompt template",
                "code": "INVALID_TEMPLATE",
            },
        )

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        http_client._TEST_TRANSPORT = None


async def test_other_refusals_are_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    rejecting_api_mock: list[dict[str, object]],
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CHATGPT_PROFILE)
    with pytest.raises(ToolError):
        await create_server().call_tool("agents.create", {"body": _base_body()})

    assert len(rejecting_api_mock) == 1, "a non-runtime refusal must reach the caller as-is"
