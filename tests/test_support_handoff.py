"""'Contact support' errors point at support.ticket.create.

Platform ends about fifteen errors in "Contact support." or "Contact
support@speko.ai". Whatever tool tripped one, the model must read a
``support=`` step naming the ticket tool and the trace id, and every other
error must reach it unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
import spekoai_mcp.support_handoff as support_handoff
from spekoai_mcp.profiles import (
    BUILDER_PROFILE,
    CHATGPT_PROFILE,
    CONNECTOR_PROFILE,
    CUSTOMER_PROFILE,
    DEFAULT_PROFILE_ENV_VAR,
    MUSE_PROFILE,
    REPLIT_PROFILE,
    profile_serves_tool,
)
from spekoai_mcp.server import INSTRUCTIONS, create_server

CONTACT_SUPPORT = {
    "error": "Provider 'nari' is not enabled. Contact support to request access.",
    "code": "PROVIDER_NOT_ENABLED",
}
PLAIN_FAILURE = {"error": "Upstream timed out.", "code": "UPSTREAM_TIMEOUT"}


def _api(monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status, json=body, headers={"x-request-id": "req_support"})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    return paths


@pytest.fixture(autouse=True)
def _reset_transport() -> Any:
    yield
    http_client._TEST_TRANSPORT = None


TOOL_SURFACES = [
    # Handwritten dotted tool that goes through action_tools.call().
    ("sessions.create", {"body": {"agentId": "agent_1"}}),
    # Manifest-generated tool (ManifestActionTool.run, no try/except of its own).
    ("agents.test_call", {"agent_id": "agent_1", "objective": "Book a table"}),
]


@pytest.mark.parametrize(("tool", "arguments"), TOOL_SURFACES)
async def test_contact_support_error_names_the_ticket_tool(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    paths = _api(monkeypatch, 403, CONTACT_SUPPORT)
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(tool, arguments)
    text = str(raised.value)
    assert paths, "the tool must have reached Platform"
    assert "Contact support to request access." in text
    assert "support=" in text
    assert "support.ticket.create" in text
    assert "trace_id req_support" in text


@pytest.mark.parametrize(("tool", "arguments"), TOOL_SURFACES)
async def test_other_errors_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    _api(monkeypatch, 500, PLAIN_FAILURE)
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(tool, arguments)
    assert "support=" not in str(raised.value)
    assert "support.ticket.create" not in str(raised.value)


async def test_ticket_tool_failure_is_not_pointed_at_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api(monkeypatch, 500, {"error": "Ticket store down. Contact support@speko.ai."})
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "support.ticket.create",
            {"summary": "Calls fail", "details": "All calls fail.", "category": "calls"},
        )
    assert "support=" not in str(raised.value)


async def test_profile_without_ticket_tool_gets_no_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _api(monkeypatch, 403, CONTACT_SUPPORT)
    monkeypatch.setattr(support_handoff, "profile_serves_tool", lambda name, profile: False)
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool("sessions.create", {"body": {"agentId": "agent_1"}})
    assert "support=" not in str(raised.value)


@pytest.mark.parametrize(
    "profile",
    [
        None,
        CUSTOMER_PROFILE,
        BUILDER_PROFILE,
        CHATGPT_PROFILE,
        CONNECTOR_PROFILE,
        REPLIT_PROFILE,
        MUSE_PROFILE,
    ],
)
def test_every_profile_serves_the_ticket_tool(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    # The server instructions name support.ticket.create on every profile.
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    assert profile_serves_tool("support.ticket.create", profile)


def test_instructions_offer_a_ticket_before_email() -> None:
    assert "support.ticket.create" in INSTRUCTIONS
    assert "instead of sending them to email" in INSTRUCTIONS


@pytest.mark.parametrize(
    "message",
    [
        "Contact support.",
        "tell the user to contact Speko support.",
        "Contact support@speko.ai.",
    ],
)
def test_every_contact_support_wording_matches(message: str) -> None:
    assert support_handoff._CONTACT_SUPPORT.search(message)
