"""The prod MCP tool_error groups of 2026-10-19..21 that were ours to fix.

Each test pins one group: a request whose meaning was unambiguous but which
the server rejected, or a customer-side refusal whose next step told the
model the wrong thing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from spekoai_mcp import http_client
from spekoai_mcp.argument_shape import normalize_arguments
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_server

SESSION_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch):
    """Answer every Platform request with `api.reply`, recording what was sent."""
    state = SimpleNamespace(
        calls=[],
        reply=(200, {"kind": "result", "data": {}, "warnings": [], "resourceLinks": []}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        state.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": json.loads(request.content or b"{}"),
                "idempotency_key": request.headers.get("idempotency-key"),
            }
        )
        status, payload = state.reply
        return httpx.Response(status, json=payload)

    # Prod (spekoai-mcp) runs the customer profile.
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "customer")
    monkeypatch.setattr(http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test"))
    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    return state


# --- argument aliases ----------------------------------------------------------


async def test_transcript_accepts_camel_case_session_id(api: Any) -> None:
    await create_server().call_tool("sessions.transcript.get", {"sessionId": SESSION_ID})
    assert api.calls[-1]["path"] == "/v1/actions/sessions.transcript.get"
    assert api.calls[-1]["body"] == {"session_id": SESSION_ID}


@pytest.mark.parametrize(
    ("tool", "sent", "expected"),
    [
        ("operations.wait", {"id": SESSION_ID}, {"operationId": SESSION_ID}),
        (
            "agents.duplicate",
            {"agent_id": "agent_1", "name": "Copy"},
            {"source_agent_id": "agent_1", "name": "Copy"},
        ),
        (
            "capabilities.describe",
            {"name": "billing.portal.create"},
            {"actionId": "billing.portal.create"},
        ),
    ],
)
async def test_unambiguous_aliases_reach_platform_renamed(
    api: Any, tool: str, sent: dict[str, Any], expected: dict[str, Any]
) -> None:
    await create_server().call_tool(tool, sent)
    assert api.calls[-1]["body"] == expected


async def test_alias_never_overrides_a_declared_argument(api: Any) -> None:
    # Both spellings sent: nothing is guessed, Platform sees the conflict.
    sent = {"session_id": SESSION_ID, "sessionId": "other"}
    await create_server().call_tool("sessions.transcript.get", sent)
    assert api.calls[-1]["body"] == sent


# --- idempotency keys ----------------------------------------------------------


async def test_repeated_secret_action_gets_a_fresh_idempotency_key(api: Any) -> None:
    mcp = create_server()
    await mcp.call_tool("billing.portal.create", {})
    await mcp.call_tool("billing.portal.create", {})
    keys = [call["idempotency_key"] for call in api.calls]
    assert len(keys) == 2
    assert all(keys)
    assert keys[0] != keys[1]


# --- capabilities.describe on a listed MCP tool ---------------------------------


async def test_describe_of_a_listed_mcp_tool_answers_locally(api: Any) -> None:
    mcp = create_server()
    result = await mcp.call_tool("capabilities.describe", {"actionId": "sessions.phone.create"})

    assert api.calls == []
    action = result.structured_content["data"]["action"]
    listed = next(t for t in await mcp.list_tools() if t.name == "sessions.phone.create")
    assert action["id"] == "sessions.phone.create"
    assert action["inputSchema"] == listed.parameters
    assert result.structured_content["warnings"][0]["code"] == "MCP_TOOL_NOT_REGISTRY_ACTION"
    assert "Call it directly" in result.content[0].text


async def test_describe_of_a_registry_action_still_goes_to_platform(api: Any) -> None:
    await create_server().call_tool("capabilities.describe", {"actionId": "operations.wait"})
    assert api.calls[-1]["path"] == "/v1/actions/capabilities.describe"


async def test_describe_of_a_tool_the_profile_hides_goes_to_platform(
    api: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The chatgpt profile does not serve audio.synthesize; Platform answers.
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    mcp = create_server()
    if "capabilities.describe" not in {t.name for t in await mcp.list_tools()}:
        pytest.skip("profile does not serve capabilities.describe")
    await mcp.call_tool("capabilities.describe", {"actionId": "audio.synthesize"})
    assert api.calls[-1]["path"] == "/v1/actions/capabilities.describe"


# --- next steps ---------------------------------------------------------------


async def test_card_required_destination_says_add_a_card_not_retry(api: Any) -> None:
    api.reply = (
        402,
        {
            "error": "Add a card to call this country.",
            "code": "DESTINATION_REQUIRES_CARD",
            "hint": "Calls to this country are not covered by free credit.",
            "billingUrl": "/settings/billing",
        },
    )
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "sessions.phone.create", {"body": {"to": "+4915112345678", "agentId": "agent_1"}}
        )
    text = str(raised.value)
    assert "add a payment card" in text
    assert http_client.BILLING_URL in text
    assert "Retry the Speko MCP request" not in text


@pytest.mark.parametrize(
    "upstream",
    [
        "All providers failed (1 attempted): elevenlabs/eleven_flash_v2_5: 422 ElevenLabs "
        'API error 404: {"detail":{"code":"voice_not_found"}}',
        "All providers failed (1 attempted): cartesia/sonic-3.5: 422 Cartesia API error "
        "400: invalid voice specification: voice ID must be a valid UUID",
    ],
)
async def test_unknown_voice_points_at_voices_list(api: Any, upstream: str) -> None:
    api.reply = (422, {"error": upstream, "code": "ALL_PROVIDERS_FAILED"})
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "audio.synthesize",
            {"body": {"text": "hi", "intent": {"language": "en"}, "voice": "EXAVITQu4vr4xnAyCHMk"}},
        )
    assert "voices.list" in str(raised.value)


async def test_builtin_config_invalid_shows_the_transfer_call_shape(api: Any) -> None:
    api.reply = (
        422,
        {
            "error": 'Invalid config for builtin "transfer_call": destinations: Invalid input',
            "code": "BUILTIN_CONFIG_INVALID",
        },
    )
    body = {
        "name": "transfer",
        "description": "Transfer the caller.",
        "parameters": {"type": "object", "properties": {}},
        "source": {"kind": "builtin", "name": "transfer_call", "config": {"phoneNumber": "+1"}},
    }
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "agents.tools.create", {"agent_id": "agent_1", "body": body}
        )
    assert "'destinations':[{'label'" in str(raised.value)


# --- request shaping ----------------------------------------------------------


async def test_available_number_search_caps_limit_at_fifty(api: Any) -> None:
    api.reply = (200, [])
    await create_server().call_tool("phone_numbers.available.search", {"limit": 100})
    assert api.calls[-1]["query"]["limit"] == "50"


async def test_phone_number_separators_are_stripped(api: Any) -> None:
    api.reply = (200, {"sessionId": "sess_1"})
    await create_server().call_tool(
        "sessions.phone.create", {"body": {"to": "+1 (201) 555-1234", "agentId": "agent_1"}}
    )
    assert api.calls[-1]["body"]["to"] == "+12015551234"


async def test_number_without_country_code_is_not_guessed(api: Any) -> None:
    with pytest.raises(ToolError, match="E.164"):
        await create_server().call_tool(
            "sessions.phone.create", {"body": {"to": "201-555-1234", "agentId": "agent_1"}}
        )
    assert api.calls == []


async def test_agent_and_call_descriptions_say_not_to_send_end_call(api: Any) -> None:
    tools = {t.name: t for t in await create_server().list_tools()}
    assert "Never send endCall" in tools["agents.update"].description
    assert "Never send endCall" in tools["agents.create"].description
    phone_body = tools["sessions.phone.create"].parameters["properties"]["body"]["description"]
    assert "Never send endCall" in phone_body
    assert len(phone_body.encode()) <= 2048
    tool_body = tools["agents.tools.create"].parameters["properties"]["body"]["description"]
    assert "destinations" in tool_body
    assert len(tool_body.encode()) <= 2048


# --- argument shape (pre-validation "Invalid arguments for tool") -------------


async def test_flat_synthesize_fields_are_wrapped_into_body(api: Any) -> None:
    api.reply = (200, {"ok": True})
    mcp = create_server()
    with pytest.raises(Exception):  # noqa: B017 - only the request shape matters here
        await mcp.call_tool(
            "audio.synthesize", {"text": "hi", "intent": {"language": "en"}, "spokenForm": True}
        )
    assert api.calls, "the call must reach the API instead of failing validation"
    assert api.calls[0]["body"]["text"] == "hi"
    assert api.calls[0]["body"]["spokenForm"] is True


async def test_flat_phone_session_fields_are_wrapped_into_body(api: Any) -> None:
    api.reply = (200, {"sessionId": "sess_1"})
    await create_server().call_tool(
        "sessions.phone.create", {"to": "+12015551234", "agentId": "agent_1"}
    )
    assert api.calls[-1]["body"] == {"to": "+12015551234", "agentId": "agent_1"}


async def test_update_agent_keeps_path_argument_and_wraps_the_rest(api: Any) -> None:
    api.reply = (200, {"id": "agent_1"})
    await create_server().call_tool(
        "agents.update", {"agentId": "agent_1", "systemPrompt": "Be brief."}
    )
    assert api.calls[-1]["path"] == "/v1/agents/agent_1"
    assert api.calls[-1]["body"] == {"systemPrompt": "Be brief."}


async def test_sessions_list_accepts_agent_id_in_camel_case(api: Any) -> None:
    api.reply = (200, {"entries": [], "nextCursor": None})
    await create_server().call_tool("sessions.list", {"agentId": "agent_1"})
    assert api.calls[-1]["query"]["agent"] == "agent_1"


@pytest.mark.parametrize(
    "arguments",
    [
        {"sessionId": SESSION_ID},
        {"id": SESSION_ID},
        {"path": {"id": SESSION_ID}},
    ],
)
async def test_sessions_get_accepts_common_id_shapes(api: Any, arguments: dict[str, Any]) -> None:
    api.reply = (200, {"id": SESSION_ID})
    await create_server().call_tool("sessions.get", arguments)
    assert api.calls[-1]["path"] == f"/v1/sessions/{SESSION_ID}"


async def test_transcribe_body_string_is_unwrapped(api: Any) -> None:
    # audio.transcribe is flat; models sent body: '{"audio_url": ...}' (26 in prod).
    tool = await create_server().get_tool("audio.transcribe")
    assert tool is not None
    reshaped = normalize_arguments(
        "audio.transcribe", tool.parameters, {"body": '{"audioUrl": "https://x.test/a.wav"}'}
    )
    assert reshaped == {"audio_url": "https://x.test/a.wav"}


async def test_country_name_is_normalized(api: Any) -> None:
    api.reply = (200, [])
    await create_server().call_tool(
        "phone_numbers.available.search", {"countryCode": "Canada", "area_code": "416"}
    )
    assert api.calls[-1]["query"]["country"] == "CA"


async def test_conflicting_or_unknown_arguments_still_fail_validation(api: Any) -> None:
    with pytest.raises(Exception, match="Unexpected keyword argument|unexpected"):
        await create_server().call_tool("voices.list", {"language": "en"})
    assert api.calls == []


async def test_synthesis_stall_says_retry_once_then_route_around(api: Any) -> None:
    api.reply = (
        504,
        {"error": "OpenAI TTS stream stalled: no audio for 15000 ms", "code": "PROVIDER_TIMEOUT"},
    )
    with pytest.raises(ToolError) as raised:
        await create_server().call_tool(
            "audio.synthesize", {"body": {"text": "hi", "intent": {"language": "en"}}}
        )
    text = str(raised.value)
    assert "Retry the same request once" in text
    assert "allowedProviders.tts" in text


def test_wrapper_that_conflicts_with_a_top_level_argument_is_left_alone() -> None:
    schema = {"properties": {"agent_id": {"type": "string"}}, "required": ["agent_id"]}
    sent = {"agent_id": "agent_A", "path": {"agent_id": "agent_B"}}
    assert normalize_arguments("agents.delete", schema, sent) == sent
