"""A caller must be able to bound and stop a directory phone call."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from spekoai_mcp import http_client
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server


@pytest.fixture
def api_calls(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append((request.method, request.url.path, body))
        return httpx.Response(
            200,
            json={
                "kind": "result",
                "data": {"sessionId": body.get("session_id"), "status": "ending"},
                "warnings": [],
                "resourceLinks": [],
            },
        )

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    return calls


@pytest.mark.parametrize("profile", ["chatgpt", "connector"])
async def test_directory_calls_have_a_five_minute_default(profile, api_calls, monkeypatch):
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    await create_server().call_tool(
        "sessions.phone.create", {"body": {"to": "+12015551234", "intent": {"language": "en"}}}
    )
    assert api_calls[-1][2]["maxDurationSeconds"] == 300


@pytest.mark.parametrize("profile", ["chatgpt", "connector", "replit"])
async def test_explicit_duration_is_preserved(profile, api_calls, monkeypatch):
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    await create_server().call_tool(
        "sessions.phone.create",
        {"body": {"to": "+12015551234", "agentId": "a", "maxDurationSeconds": 600}},
    )
    assert api_calls[-1][2]["maxDurationSeconds"] == 600


async def test_direct_mcp_call_retains_its_existing_default(api_calls, monkeypatch):
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    await create_server().call_tool(
        "sessions.phone.create", {"body": {"to": "+12015551234", "agentId": "a"}}
    )
    assert "maxDurationSeconds" not in api_calls[-1][2]


@pytest.mark.parametrize("profile", ["chatgpt", "connector", "replit", "customer", None])
async def test_a_live_session_can_be_ended_through_mcp(profile, api_calls, monkeypatch):
    if profile:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    else:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    session_id = "00000000-0000-4000-8000-000000000001"
    mcp = create_server()
    result = await mcp.call_tool("sessions.end", {"session_id": session_id})
    assert result.structured_content["data"]["status"] == "ending"
    assert api_calls == [("POST", "/v1/actions/sessions.end", {"session_id": session_id})]
    tool = next(tool for tool in await mcp.list_tools() if tool.name == "sessions.end")
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is True


async def test_phone_tool_advertises_duration_and_hangup(monkeypatch):
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    tool = next(
        tool for tool in await create_server().list_tools() if tool.name == "sessions.phone.create"
    )
    assert "maxDurationSeconds" in json.dumps(tool.parameters)
    assert "sessions.end" in tool.description
