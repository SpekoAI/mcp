"""The voicemail notice on `sessions.phone.create` results for calls with no agent."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

import spekoai_mcp.http_client as http_client
import spekoai_mcp.profiles as profiles
import spekoai_mcp.voicemail_notice as voicemail_notice
from spekoai_mcp import attribution
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server
from spekoai_mcp.voicemail_notice import (
    NOTICE_INTERVAL_SECONDS,
    VOICEMAIL_GREETING_TEXT,
    VOICEMAIL_NOTICE_ENV_VAR,
    VOICEMAIL_NOTICE_TEXT,
    _ShownLog,
    with_voicemail_notice,
)

ORG = "org_1"
NO_AGENT_CALL = {"to": "+12015551234", "intent": {"language": "en"}}


@pytest.fixture(autouse=True)
def fresh_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voicemail_notice, "_SHOWN", _ShownLog())
    monkeypatch.setenv(VOICEMAIL_NOTICE_ENV_VAR, "true")


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(attribution, "submit_event", captured.append)
    return captured


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(reply=(201, {"sessionId": "sess_1"}))

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = state.reply
        return httpx.Response(status, json=body, headers={"x-request-id": "req_1"})

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield state
    finally:
        http_client._TEST_TRANSPORT = None


def _use(monkeypatch: pytest.MonkeyPatch, profile: str, organization_id: str | None = ORG) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    claims: dict[str, str] = {"auth_method": "oauth"}
    if organization_id:
        claims["organization_id"] = organization_id
    token = SimpleNamespace(claims=claims, client_id="oauth-client", subject="user_1")
    monkeypatch.setattr(profiles, "get_access_token", lambda: token)
    monkeypatch.setattr(voicemail_notice, "get_access_token", lambda: token)


async def _call(body: dict[str, Any]) -> str:
    out = await create_server().call_tool("sessions.phone.create", {"body": dict(body)})
    return out.content[0].text


@pytest.mark.parametrize(
    ("profile", "greeting"),
    [("customer", True), ("muse", True), ("connector", False), ("chatgpt", False)],
)
async def test_call_with_no_agent_gets_the_notice_once(
    monkeypatch: pytest.MonkeyPatch,
    platform: SimpleNamespace,
    events: list[dict[str, Any]],
    profile: str,
    greeting: bool,
) -> None:
    _use(monkeypatch, profile)

    first = await _call(NO_AGENT_CALL)
    second = await _call(NO_AGENT_CALL)

    assert VOICEMAIL_NOTICE_TEXT in first
    assert (VOICEMAIL_GREETING_TEXT in first) is greeting
    assert "sess_1" in first
    assert VOICEMAIL_NOTICE_TEXT not in second
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "mcp_notice_exposure"
    assert event["notice"] == "inbound_voicemail_v1"
    assert event["organization_id"] == ORG
    assert event["tool"] == "sessions.phone.create"
    assert event["greeting_tool"] is greeting


async def test_call_with_an_agent_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace, events: list[dict[str, Any]]
) -> None:
    """The number answers with that agent, so voicemail does not apply."""
    _use(monkeypatch, "customer")

    text = await _call({"to": "+12015551234", "agentId": "agent_1"})

    assert VOICEMAIL_NOTICE_TEXT not in text
    assert events == []


async def test_notice_follows_the_free_call_note(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace, events: list[dict[str, Any]]
) -> None:
    _use(monkeypatch, "connector")
    platform.reply = (201, {"sessionId": "sess_1", "freeCall": {"maxDurationSeconds": 120}})

    text = await _call({"to": "+8613800138000", "intent": {"language": "en"}})

    assert "one free call abroad" in text
    assert text.index("one free call abroad") < text.index(VOICEMAIL_NOTICE_TEXT)


@pytest.mark.parametrize("value", [None, "", "false", "0", "off"])
async def test_flag_off_gets_nothing(
    monkeypatch: pytest.MonkeyPatch,
    platform: SimpleNamespace,
    events: list[dict[str, Any]],
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv(VOICEMAIL_NOTICE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(VOICEMAIL_NOTICE_ENV_VAR, value)
    _use(monkeypatch, "customer")

    assert VOICEMAIL_NOTICE_TEXT not in await _call(NO_AGENT_CALL)
    assert events == []


async def test_no_organization_gets_nothing(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace, events: list[dict[str, Any]]
) -> None:
    _use(monkeypatch, "customer", organization_id=None)

    assert VOICEMAIL_NOTICE_TEXT not in await _call(NO_AGENT_CALL)
    assert events == []


async def test_failed_call_gets_nothing_and_does_not_use_up_the_notice(
    monkeypatch: pytest.MonkeyPatch, platform: SimpleNamespace, events: list[dict[str, Any]]
) -> None:
    _use(monkeypatch, "customer")
    platform.reply = (402, {"error": "No credit.", "code": "INSUFFICIENT_CREDITS"})
    with pytest.raises(ToolError) as raised:
        await _call(NO_AGENT_CALL)
    assert VOICEMAIL_NOTICE_TEXT not in str(raised.value)

    platform.reply = (201, {"sessionId": "sess_1"})
    assert VOICEMAIL_NOTICE_TEXT in await _call(NO_AGENT_CALL)
    assert len(events) == 1


def test_shown_again_after_the_interval_and_per_organization() -> None:
    now = [1_000.0]
    log = _ShownLog(clock=lambda: now[0])

    assert log.claim("org_a") is True
    assert log.claim("org_a") is False
    assert log.claim("org_b") is True
    now[0] += NOTICE_INTERVAL_SECONDS - 1
    assert log.claim("org_a") is False
    now[0] += 1
    assert log.claim("org_a") is True


def test_a_failure_inside_the_notice_never_changes_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("token lookup failed")

    monkeypatch.setattr(voicemail_notice, "get_access_token", boom)
    original = ToolResult(content=[], structured_content={"sessionId": "sess_1"})

    assert with_voicemail_notice(original, NO_AGENT_CALL) is original
