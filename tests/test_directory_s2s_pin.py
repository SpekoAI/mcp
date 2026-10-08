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
    DIRECTORY_S2S_PIN,
    INCOMPATIBLE_RUNTIME_CODE,
    apply_directory_phone_s2s_pin,
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


@pytest.mark.parametrize("language", ["en", "hi", "es", "uz", "ja", "en-GB", "nb-NO"])
def test_pin_applies_in_every_language(monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    """Reversal of #2693 (2026-09-17): no language guard.

    0.2.27 kept the routed cascade for a language outside the catalog's
    `languages.s2s`. The operator decision is that directory traffic runs
    GPT-Live in every language; a caller who wants the cascade dials an agent
    saved that way.
    """
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = apply_directory_s2s_pin(_body_in(language))
    assert body["runMode"] == "s2s", f"{language} must ride GPT-Live like every other language"
    assert body["stackPreferences"]["allowedProviders"]["s2s"] == [DIRECTORY_S2S_PIN]


def test_a_body_with_no_language_gets_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: CHATGPT_PROFILE)
    body = _base_body()
    del body["intent"]
    assert apply_directory_s2s_pin(body)["runMode"] == "s2s"


def test_no_language_set_survives_the_reversal() -> None:
    """The set was to be deleted together with the guard; it is."""
    assert not hasattr(action_tools, "DIRECTORY_S2S_LANGUAGES")


# --- Agentless phone calls -------------------------------------------------


def _phone_body() -> dict[str, object]:
    return {
        "to": "+998901234567",
        "intent": {"language": "uz"},
        "systemPrompt": "You are Ava from Northside Clinic.",
    }


@pytest.mark.parametrize("profile", sorted(DIRECTORY_PROFILES))
def test_agentless_phone_call_is_stamped_speech_native_on_every_directory_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: profile)
    body = apply_directory_phone_s2s_pin(_phone_body())
    assert body["runMode"] == "s2s"


def test_phone_call_naming_an_agent_keeps_that_agent_s_run_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent row decides; an agent created in the dashboard is the person's call."""
    monkeypatch.setattr(action_tools, "current_profile", lambda: CONNECTOR_PROFILE)
    body = {"to": "+12015551234", "agentId": "agent_1"}
    assert apply_directory_phone_s2s_pin(body) == {"to": "+12015551234", "agentId": "agent_1"}


def test_phone_call_with_an_explicit_run_mode_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: CONNECTOR_PROFILE)
    body = _phone_body()
    body["runMode"] = "cascade"
    assert apply_directory_phone_s2s_pin(body)["runMode"] == "cascade"


def test_phone_pin_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: CONNECTOR_PROFILE)
    once = apply_directory_phone_s2s_pin(_phone_body())
    twice = apply_directory_phone_s2s_pin(dict(once))
    assert once == twice


def test_phone_pin_is_scoped_to_directory_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(action_tools, "current_profile", lambda: "builder")
    assert "runMode" not in apply_directory_phone_s2s_pin(_phone_body())


@pytest.fixture
def phone_api_mock(monkeypatch: pytest.MonkeyPatch):
    """Capture what the relay actually sends to POST /v1/sessions/phone."""
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content.decode("utf-8") or "{}"),
            }
        )
        return httpx.Response(
            200,
            json={
                "sessionId": "sess_1",
                "callControlId": "sip_1",
                "roomName": "speko_sess_1",
                "status": "dialing",
                "to": "+998901234567",
                "from": "+12015550000",
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


async def test_connector_agentless_phone_call_rides_gpt_live_on_the_wire(
    phone_api_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body the connector's own dial puts on the wire says `runMode: 's2s'`.

    2026-09-17: org 9b2baeb7's connector calls to Uzbekistan ran a routed
    cascade (elevenlabs / baseten / soniox) because an agentless phone body
    had no way to ask for GPT-Live. Now it does, and the relay asks.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CONNECTOR_PROFILE)

    await create_server().call_tool(
        "sessions.phone.create",
        {"body": {"to": "+998901234567", "intent": {"language": "uz"}}},
    )

    sent = [call for call in phone_api_mock if call["path"] == "/v1/sessions/phone"]
    assert len(sent) == 1, phone_api_mock
    assert sent[0]["body"]["runMode"] == "s2s"
    assert "agentId" not in sent[0]["body"]


async def test_connector_phone_call_to_an_agent_sends_no_run_mode(
    phone_api_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, CONNECTOR_PROFILE)

    await create_server().call_tool(
        "sessions.phone.create",
        {"body": {"to": "+12015551234", "agentId": "agent_1"}},
    )

    sent = [call for call in phone_api_mock if call["path"] == "/v1/sessions/phone"]
    assert len(sent) == 1, phone_api_mock
    assert "runMode" not in sent[0]["body"]


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
