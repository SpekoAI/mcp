"""Transport failures on the Platform relay.

Prod in the week to 2026-11-20: 67 MCP 504s at exactly 300 s (the linked
synthesis read timeout equalled Cloud Run's request timeout, so Cloud Run cut
the request first) and 114 errors whose message ended "Unable to reach SpekoAI
API at ...: " with nothing after it, because httpx timeouts stringify to "".
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.action_tools as action_tools
import spekoai_mcp.http_client as http_client
from spekoai_mcp.server import create_server


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Route every Platform request through `state.handler`, counting attempts."""
    state = SimpleNamespace(attempts=0, handler=None)

    def handler(request: httpx.Request) -> httpx.Response:
        state.attempts += 1
        return state.handler(request)

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    return state


async def test_a_connect_timeout_is_retried_once(transport: SimpleNamespace) -> None:
    """Nothing was sent, so a second attempt is safe even for a POST."""
    outcomes = iter([httpx.ConnectTimeout(""), httpx.Response(200, json={"id": "agent_1"})])

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    transport.handler = handler
    payload = await http_client.call_speko_api("POST", "/v1/agents", {"name": "a"})
    assert payload == {"id": "agent_1"}
    assert transport.attempts == 2


async def test_a_second_connect_timeout_names_the_exception(transport: SimpleNamespace) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    transport.handler = handler
    with pytest.raises(ToolError) as error:
        await create_server().call_tool("agents.list", {})
    assert transport.attempts == 2
    assert "Unable to reach SpekoAI API at" in str(error.value)
    assert "ConnectTimeout" in str(error.value)


async def test_a_read_timeout_is_not_retried(transport: SimpleNamespace) -> None:
    """The server may have acted on the request, so a read timeout is final."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    transport.handler = handler
    with pytest.raises(http_client.SpekoApiError) as error:
        await http_client.call_speko_api_raw("POST", "/v1/synthesize", {"text": "hi"})
    assert transport.attempts == 1
    assert error.value.message.endswith("did not answer in time (ReadTimeout)")
    assert error.value.code == http_client.READ_TIMEOUT_CODE


def test_a_synthesis_read_timeout_says_not_to_resend() -> None:
    """The server may still finish and bill the clip; a resend bills it twice."""
    exc = http_client.SpekoApiError(0, "late", code=http_client.READ_TIMEOUT_CODE)
    step = action_tools.next_step_for_error(exc, path="/v1/synthesize")
    assert step == action_tools.SYNTHESIZE_READ_TIMEOUT_NEXT_STEP
    assert "bill it twice" in step
    assert "Ask the user before sending it again" in step
    assert "Retry the Speko MCP request" not in step


async def test_a_connect_failure_after_a_redirect_is_not_retried(
    transport: SimpleNamespace,
) -> None:
    """The first hop answered, so the server may have acted on the POST."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/phone":
            return httpx.Response(303, headers={"location": "https://elsewhere.invalid/status"})
        raise httpx.ConnectError("refused", request=request)

    transport.handler = handler
    with pytest.raises(http_client.SpekoApiError):
        await http_client.call_speko_api("POST", "/v1/sessions/phone", {"to": "+1"})
    assert transport.attempts == 2  # the POST once, the redirect target once


async def test_each_phase_has_its_own_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Timeout] = []

    class RecordingClient(httpx.AsyncClient):
        def __init__(self, *, timeout: httpx.Timeout, **kwargs: object) -> None:
            seen.append(timeout)
            super().__init__(
                timeout=timeout,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            )

    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    monkeypatch.setattr(http_client.httpx, "AsyncClient", RecordingClient)
    await http_client.call_speko_api_raw(
        "POST", "/v1/synthesize", {}, timeout=action_tools._LINKED_SYNTHESIS_TIMEOUT_S
    )
    assert seen == [httpx.Timeout(connect=10.0, read=285.0, write=30.0, pool=10.0)]


def test_linked_synthesis_ends_before_cloud_runs_request_timeout() -> None:
    assert action_tools._LINKED_SYNTHESIS_TIMEOUT_S < 300.0
