"""Every failed tool call leaves one WARNING line with its reason.

FastMCP logs a ToolError as a bare "Error calling tool X". The 2026-10-04..11
prod audit could not root-cause two open issues because of it: status, code
and message reached the model and no log. These tests pin the replacement line
and what it must never carry.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
from spekoai_mcp.server import create_server
from spekoai_mcp.tool_error_log import MAX_MESSAGE_CHARS, safe_message

LOGGER = "spekoai_mcp.tool_error_log"


def _api(monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"x-request-id": "req_log"})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-secret-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _reset_transport() -> Any:
    yield
    http_client._TEST_TRANSPORT = None


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


TOOL_SURFACES = [
    # Handwritten dotted tool: action_tools.call() -> tool_error().
    ("sessions.create", {"body": {"agentId": "agent_1"}}),
    # Manifest-generated tool: SpekoApiError escapes and FastMCP wraps it.
    ("agents.test_call", {"agent_id": "agent_1", "objective": "Book a table"}),
]


@pytest.mark.parametrize(("tool", "arguments"), TOOL_SURFACES)
async def test_api_error_logs_status_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    _api(
        monkeypatch,
        403,
        {
            "error": "This connection is not authorized for phone calling.",
            "code": "PHONE_NUMBER_SCOPE_REQUIRED",
        },
    )
    caplog.set_level(logging.WARNING, logger=LOGGER)
    with pytest.raises(ToolError):
        await create_server().call_tool(tool, arguments)

    lines = _lines(caplog)
    assert lines == [
        f"tool_error tool={tool} status=403 code=PHONE_NUMBER_SCOPE_REQUIRED "
        "trace_id=req_log message=This connection is not authorized for phone calling."
    ]
    # Arguments and the bearer token stay out of the line.
    assert "Book a table" not in lines[0]
    assert "agent_1" not in lines[0]
    assert "sk_test" not in lines[0]


async def test_tool_raised_validation_error_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A ToolError raised by our own code before any API call still names why.
    caplog.set_level(logging.WARNING, logger=LOGGER)
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "audio.transcribe", {"audio_url": "http://example.com/a.wav?sig=SECRET"}
        )
    lines = _lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("tool_error tool=audio.transcribe status=- code=- trace_id=- ")
    assert str(raised.value)[:40] in lines[0]
    assert "SECRET" not in lines[0]


async def test_successful_call_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _api(monkeypatch, 200, {"id": "sess_1"})
    caplog.set_level(logging.WARNING, logger=LOGGER)
    await create_server().call_tool("sessions.create", {"body": {"agentId": "agent_1"}})
    assert _lines(caplog) == []


def test_message_masks_query_strings_and_numbers_and_truncates() -> None:
    text = safe_message(
        "Fetch of https://storage.example.com/rec/a.wav?X-Goog-Signature=abc123 failed;\n"
        "dialing +14155550123 refused. " + "x" * 400
    )
    assert "abc123" not in text
    assert "https://storage.example.com/rec/a.wav?[redacted]" in text
    assert "+14155550123" not in text
    assert "+***0123" in text
    assert "\n" not in text
    assert len(text) == MAX_MESSAGE_CHARS
    assert text.endswith("...")
