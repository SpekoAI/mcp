"""Out-of-credit rendering across every tool surface.

Platform answers ``402 {error, code: INSUFFICIENT_CREDITS, balanceUsd}`` from
its pre-flight credit gates. Whatever tool tripped it, the model must read one
message that names the cause, the billing page and "do not retry", never
``next_step=Retry the Speko MCP request``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import CREDIT_EXHAUSTED_NEXT_STEP, next_step_for_error
from spekoai_mcp.server import create_server

OUT_OF_CREDIT = {
    "error": "Insufficient credit balance. Add credits before starting a session.",
    "code": "INSUFFICIENT_CREDITS",
    "balanceUsd": -0.12,
    "currency": "USD",
}


@pytest.fixture
def out_of_credit_api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every Platform call answers 402 INSUFFICIENT_CREDITS, except the balance read."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/actions/credits.balance.get":
            return httpx.Response(
                200,
                json={"kind": "result", "data": {"balanceUsd": -0.12, "currency": "USD"}},
            )
        return httpx.Response(402, json=OUT_OF_CREDIT, headers={"x-request-id": "req_402"})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield paths
    finally:
        http_client._TEST_TRANSPORT = None


def _assert_out_of_credit_message(text: str) -> None:
    assert "out of credit" in text
    assert "(balance $-0.12)" in text
    assert "402 INSUFFICIENT_CREDITS" in text
    assert http_client.BILLING_URL in text
    assert "trace_id=req_402" in text
    assert "Retry the Speko MCP request" not in text
    assert "Error calling tool" not in text


def test_api_error_carries_code_and_balance() -> None:
    resp = httpx.Response(402, json=OUT_OF_CREDIT, request=httpx.Request("POST", "https://x"))
    with pytest.raises(http_client.SpekoApiError) as raised:
        http_client._raise_api_error(resp)
    exc = raised.value
    assert exc.status_code == 402
    assert exc.code == "INSUFFICIENT_CREDITS"
    assert exc.balance_usd == -0.12
    assert exc.credit_exhausted
    assert exc.message == OUT_OF_CREDIT["error"]


def test_actions_error_shape_carries_nested_code() -> None:
    body = {"error": {"code": "INSUFFICIENT_CREDITS", "message": "Insufficient credit balance."}}
    resp = httpx.Response(402, json=body, request=httpx.Request("POST", "https://x"))
    with pytest.raises(http_client.SpekoApiError) as raised:
        http_client._raise_api_error(resp)
    assert raised.value.code == "INSUFFICIENT_CREDITS"
    assert raised.value.credit_exhausted


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (402, None, True),  # bare 402 from Platform
        (402, "INSUFFICIENT_CREDITS", True),
        (402, "PROVIDER_PAYMENT_REQUIRED", False),  # a proxied provider billing failure
        (400, "INSUFFICIENT_CREDITS", True),  # the code is authoritative
        (500, None, False),
    ],
)
def test_credit_exhausted_predicate(status: int, code: str | None, expected: bool) -> None:
    assert http_client.SpekoApiError(status, "x", code=code).credit_exhausted is expected


def test_next_step_for_402_says_top_up_not_retry() -> None:
    exc = http_client.SpekoApiError(
        402, "Insufficient credit balance.", code="INSUFFICIENT_CREDITS"
    )
    assert next_step_for_error(exc, path="/v1/sessions") == CREDIT_EXHAUSTED_NEXT_STEP
    assert http_client.BILLING_URL in CREDIT_EXHAUSTED_NEXT_STEP
    assert "Do not retry" in CREDIT_EXHAUSTED_NEXT_STEP


def test_tool_error_message_short_circuits_for_credit_exhaustion() -> None:
    exc = http_client.SpekoApiError(
        402,
        "Insufficient credit balance.",
        trace_id="req_402",
        code="INSUFFICIENT_CREDITS",
        balance_usd=-0.12,
    )
    text = http_client.tool_error_message(exc, next_step="Retry the Speko MCP request.")
    _assert_out_of_credit_message(text)
    # Non-credit errors keep the classic shape.
    other = http_client.SpekoApiError(500, "boom", trace_id="t")
    assert http_client.tool_error_message(other, next_step="n") == (
        "Speko API returned 500: boom; trace_id=t; next_step=n"
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        # Handwritten dotted tool that goes through action_tools.call().
        ("sessions.create", {"body": {"agentId": "agent_1"}}),
        # Audio tool that calls http_client directly (previously escaped to FastMCP).
        ("audio.synthesize", {"body": {"text": "hi", "intent": {"language": "en"}}}),
        # Manifest-generated tool (ManifestActionTool.run, no try/except of its own).
        ("agents.test_call", {"agent_id": "agent_1", "objective": "Book a table"}),
    ],
)
async def test_every_tool_surface_renders_the_same_out_of_credit_message(
    out_of_credit_api: list[str], tool: str, arguments: dict[str, Any]
) -> None:
    mcp = create_server()
    with pytest.raises(ToolError) as raised:
        await mcp.call_tool(tool, arguments)
    _assert_out_of_credit_message(str(raised.value))
    assert out_of_credit_api, "the tool must have reached Platform"


async def test_balance_read_names_the_billing_page_when_exhausted(
    out_of_credit_api: list[str],
) -> None:
    mcp = create_server()
    result = await mcp.call_tool("credits.balance.get", {})
    text = result.content[0].text  # type: ignore[union-attr]
    assert "$-0.12" in text
    assert "out of credit" in text
    assert http_client.BILLING_URL in text
    structured = json.loads(json.dumps(result.structured_content))
    assert structured["data"]["balanceUsd"] == -0.12
    assert "/v1/actions/credits.balance.get" in out_of_credit_api
