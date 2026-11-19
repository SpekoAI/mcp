"""A call placed on the user's behalf can bring the user in (`handoff`)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from spekoai_mcp import http_client
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import INSTRUCTIONS, create_server


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"sessionId": "sess_1", "status": "dialing"})

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    return bodies


@pytest.mark.parametrize("profile", ["chatgpt", "connector"])
async def test_a_directory_handoff_call_waits_out_a_hold_queue(profile, sent, monkeypatch):
    # Five minutes is shorter than the bank hold the feature exists for.
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    await create_server().call_tool(
        "sessions.phone.create",
        {
            "body": {
                "to": "+12015551234",
                "intent": {"language": "en"},
                "handoff": {"to": "+12015550123"},
            }
        },
    )
    assert sent[-1]["maxDurationSeconds"] == 1800


async def test_handoff_number_is_sent_without_separators(sent, monkeypatch):
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    await create_server().call_tool(
        "sessions.phone.create",
        {
            "body": {
                "to": "+12015551234",
                "intent": {"language": "en"},
                "handoff": {"to": "+998 97 123 45 67", "name": "Bek"},
            }
        },
    )
    assert sent[-1]["handoff"] == {"to": "+998971234567", "name": "Bek"}


@pytest.mark.parametrize("profile", ["chatgpt", "connector", "gemini", "muse", None])
async def test_every_calling_surface_tells_the_model_to_ask(profile, monkeypatch):
    if profile:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    else:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    tool = next(t for t in await create_server().list_tools() if t.name == "sessions.phone.create")
    assert "body.handoff.to" in tool.description
    assert "account holder" in tool.description
    assert "handoff" in tool.parameters["properties"]["body"]["description"]


def test_server_instructions_name_the_handoff_field():
    assert "handoff.to on sessions.phone.create" in INSTRUCTIONS
