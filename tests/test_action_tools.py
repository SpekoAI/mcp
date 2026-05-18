from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.server import create_server


@pytest.fixture
def speko_api_mock(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}")
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "auth": request.headers.get("authorization"),
                "body": body,
            }
        )
        path = request.url.path
        if path == "/v1/credits/balance":
            return json_response(
                {"balanceUsd": 7, "currency": "USD", "updatedAt": "2026-05-15T00:00:00.000Z"}
            )
        if path == "/v1/inference/inspect":
            return json_response(
                {"detected_providers": ["livekit"], "speko_recommendation": "migrate"}
            )
        if path == "/v1/inference/sessionconfig":
            return json_response(build_payload())
        if path == "/v1/inference/parse-config":
            return json_response(build_payload(source="vapi"))
        if path == "/v1/agents":
            return json_response({"id": "agent_1", "name": "Demo"})
        if path == "/v1/agents/agent_1/deploy":
            return json_response({"agent_id": "agent_1", "version_number": 2, "status": "live"})
        if path == "/v1/agents/agent_1/rollback":
            return json_response({"agent_id": "agent_1", "version_number": 3, "status": "live"})
        if path == "/v1/sessions":
            return json_response(
                {"sessionId": "call_1", "conversationToken": "tok", "livekitUrl": "wss://lk"}
            )
        if path == "/v1/agents/agent_1/calls":
            return json_response({"calls": [{"id": "call_1", "status": "ended"}]})
        if path == "/v1/calls/call_1":
            return json_response({"id": "call_1", "transcript": {"entries": []}})
        if path == "/v1/agents/agent_1/evals":
            if request.method == "GET":
                return json_response({"evals": [{"id": "eval_1", "name": "Regression"}]})
            return json_response({"id": "eval_1", "name": body["name"]})
        if path == "/v1/agents/agent_1/evals/eval_1/run":
            return json_response({"id": "run_1", "status": "queued"})
        if path == "/v1/inference/briefing":
            return json_response({"rendered_markdown": "# Briefing", "version_number": 2})
        if path == "/v1/share/build/build_1/card.png":
            return json_response({"png_url": "https://api.speko.dev/v1/share/build/token.png"})
        return json_response({"error": f"unexpected path {path}"}, status_code=404)

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        http_client._TEST_TRANSPORT = None


async def test_private_action_tools_cover_expected_api_paths(
    speko_api_mock: list[dict[str, object]],
    tmp_path,
) -> None:
    config = tmp_path / "vapi.json"
    config.write_text('{"name":"Demo","tools":[{"name":"lookup"}]}', encoding="utf-8")
    mcp = create_server(include_private_tools=True)

    await mcp.call_tool("get_balance", {})
    await mcp.call_tool("speko_inspect", {"workspace_root": ".", "deep": False})
    await mcp.call_tool("speko_build", {"prose": "A support agent", "deploy": True})
    await mcp.call_tool("speko_migrate", {"from_platform": "vapi", "config_path": str(config)})
    await mcp.call_tool("speko_deploy", {"agent_id": "agent_1", "session_config": session_config()})
    await mcp.call_tool("speko_rollback", {"agent_id": "agent_1", "target_version_number": 1})
    await mcp.call_tool("speko_test", {"agent_id": "agent_1"})
    await mcp.call_tool("speko_logs", {"agent_id": "agent_1"})
    call_result = await mcp.call_tool("speko_calls_get", {"call_id": "call_1"})
    await mcp.call_tool("speko_evals_list", {"agent_id": "agent_1"})
    await mcp.call_tool("speko_evals_run", {"agent_id": "agent_1", "eval_id": "eval_1"})
    await mcp.call_tool("speko_evals_add_from_call", {"agent_id": "agent_1", "call_id": "call_1"})
    await mcp.call_tool("speko_briefing", {"agent_id": "agent_1"})
    share_result = await mcp.call_tool("speko_share", {"build_id": "build_1"})
    await mcp.call_tool("speko_build_and_test", {"prose": "A support agent"})
    await mcp.call_tool(
        "speko_migrate_and_deploy", {"from_platform": "vapi", "config_path": str(config)}
    )

    paths = {(call["method"], call["path"]) for call in speko_api_mock}
    assert ("GET", "/v1/credits/balance") in paths
    assert ("POST", "/v1/inference/inspect") in paths
    assert ("POST", "/v1/inference/sessionconfig") in paths
    assert ("POST", "/v1/inference/parse-config") in paths
    assert ("POST", "/v1/agents") in paths
    assert ("POST", "/v1/agents/agent_1/deploy") in paths
    assert ("POST", "/v1/agents/agent_1/rollback") in paths
    assert ("POST", "/v1/sessions") in paths
    assert ("GET", "/v1/agents/agent_1/calls") in paths
    assert ("GET", "/v1/calls/call_1") in paths
    assert ("GET", "/v1/agents/agent_1/evals") in paths
    assert ("POST", "/v1/agents/agent_1/evals/eval_1/run") in paths
    assert ("POST", "/v1/agents/agent_1/evals") in paths
    assert ("POST", "/v1/inference/briefing") in paths
    assert ("POST", "/v1/share/build/build_1/card.png") in paths
    assert {call["auth"] for call in speko_api_mock} == {"Bearer test-token"}
    assert any(getattr(item, "type", None) == "resource_link" for item in call_result.content)
    assert share_result.structured_content["png_url"].endswith(".png")


async def test_private_server_lists_all_action_tools() -> None:
    mcp = create_server(include_private_tools=True)
    names = {tool.name for tool in await mcp.list_tools()}
    assert set(ACTION_TOOL_NAMES).issubset(names)


def json_response(payload: dict[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def session_config() -> dict[str, object]:
    return {
        "name": "Demo",
        "systemPrompt": "Be helpful.",
        "intent": {"language": "en"},
    }


def build_payload(source: str = "prose") -> dict[str, object]:
    return {
        "session_config": session_config(),
        "agent_create_payload": {
            "name": "Demo",
            "systemPrompt": "Be helpful.",
            "intent": {"language": "en"},
        },
        "briefing_markdown": "# Briefing",
        "source": source,
        "unmappable_tools": [],
    }
