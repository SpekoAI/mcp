from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

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
                "query": request.url.query.decode("utf-8"),
                "auth": request.headers.get("authorization"),
                "body": body,
            }
        )
        path = request.url.path
        method = request.method
        if method == "GET" and path in LIST_PATHS:
            return json_response([])
        if path == "/v1/phone-numbers/available":
            return json_response([])
        if path == "/v1/credits/balance":
            return json_response(
                {"balanceUsd": 7, "currency": "USD", "updatedAt": "2026-05-15T00:00:00.000Z"}
            )
        if path == "/v1/credits/ledger":
            return json_response({"entries": [], "nextCursor": None})
        if path == "/v1/usage":
            return json_response({"totalSessions": 0, "breakdown": []})
        if path == "/v1/sessions":
            if method == "GET":
                return json_response({"entries": [], "nextCursor": None})
            return json_response({"sessionId": "sess_1", "conversationToken": "tok"})
        if path == "/v1/agents/agent_1/calls":
            return json_response({"calls": [], "entries": []})
        if path == "/v1/agents/agent_1/evals":
            if method == "GET":
                return json_response({"evals": [], "entries": []})
            return json_response({"id": "eval_1", **body})
        if path == "/v1/share/build/build_1/card.png":
            return json_response({"png_url": "https://api.speko.dev/v1/share/build/token.png"})
        return json_response(default_payload(path, method, body))

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        http_client._TEST_TRANSPORT = None


LIST_PATHS = {
    "/v1/agents",
    "/v1/agents/agent_1/tools",
    "/v1/agents/agent_1/versions",
    "/v1/phone-numbers",
    "/v1/knowledge-bases",
    "/v1/knowledge-bases/kb_1/documents",
}


async def test_action_tools_cover_expected_api_paths(
    speko_api_mock: list[dict[str, object]],
    tmp_path,
) -> None:
    mcp = create_server()
    await mcp.call_tool("get_organization", {})
    await mcp.call_tool("get_credit_balance", {})
    await mcp.call_tool("list_credit_ledger", {"limit": 25, "kind": "grant,debit"})
    await mcp.call_tool("get_usage_summary", {"from_": "2026-05-01T00:00:00.000Z"})
    await mcp.call_tool("list_agents", {})
    await mcp.call_tool("create_agent", {"body": agent_body()})
    await mcp.call_tool("get_agent", {"agent_id": "agent_1"})
    await mcp.call_tool("update_agent", {"agent_id": "agent_1", "body": {"name": "Demo v2"}})
    await mcp.call_tool("delete_agent", {"agent_id": "agent_1"})
    await mcp.call_tool("list_agent_tools", {"agent_id": "agent_1"})
    await mcp.call_tool("create_agent_tool", {"agent_id": "agent_1", "body": tool_body()})
    await mcp.call_tool("get_agent_tool", {"agent_id": "agent_1", "tool_id": "tool_1"})
    await mcp.call_tool(
        "update_agent_tool",
        {"agent_id": "agent_1", "tool_id": "tool_1", "body": {"description": "Updated"}},
    )
    await mcp.call_tool("delete_agent_tool", {"agent_id": "agent_1", "tool_id": "tool_1"})
    await mcp.call_tool(
        "deploy_agent",
        {"agent_id": "agent_1", "session_config": session_config(), "source": "test"},
    )
    await mcp.call_tool("rollback_agent", {"agent_id": "agent_1", "target_version_number": 1})
    await mcp.call_tool("list_agent_versions", {"agent_id": "agent_1"})
    await mcp.call_tool(
        "create_session", {"body": {"mode": "cascade", "intent": {"language": "en"}}}
    )
    await mcp.call_tool(
        "create_phone_session",
        {"body": {"to": "+12015550123", "intent": {"language": "en"}}},
    )
    await mcp.call_tool("list_sessions", {"limit": 10, "agent": "agent_1"})
    await mcp.call_tool("get_session", {"session_id": "sess_1"})
    await mcp.call_tool("get_session_transcript", {"session_id": "sess_1"})
    await mcp.call_tool("get_session_recording", {"session_id": "sess_1"})
    await mcp.call_tool(
        "list_agent_calls", {"agent_id": "agent_1", "since": "2026-05-01T00:00:00.000Z"}
    )
    await mcp.call_tool("get_call", {"call_id": "call_1"})
    await mcp.call_tool("get_call_recording", {"call_id": "call_1"})
    await mcp.call_tool("list_phone_numbers", {})
    await mcp.call_tool("search_available_phone_numbers", {"area_code": "415", "limit": 2})
    await mcp.call_tool("create_phone_number", {"body": {"e164": "+12015550123"}})
    await mcp.call_tool("get_phone_number", {"phone_number_id": "pn_1"})
    await mcp.call_tool(
        "update_phone_number", {"phone_number_id": "pn_1", "body": {"label": "Main"}}
    )
    await mcp.call_tool("delete_phone_number", {"phone_number_id": "pn_1"})
    await mcp.call_tool(
        "create_knowledge_base", {"body": {"agentId": "agent_1", "name": "Default"}}
    )
    await mcp.call_tool("list_knowledge_bases", {"agent_id": "agent_1"})
    await mcp.call_tool("get_knowledge_base", {"knowledge_base_id": "kb_1"})
    await mcp.call_tool("delete_knowledge_base", {"knowledge_base_id": "kb_1"})
    await mcp.call_tool("list_knowledge_documents", {"knowledge_base_id": "kb_1"})
    await mcp.call_tool(
        "create_knowledge_document",
        {
            "knowledge_base_id": "kb_1",
            "body": {"filename": "faq.md", "contentType": "text/markdown", "sizeBytes": 12},
        },
    )
    await mcp.call_tool(
        "get_knowledge_document",
        {"knowledge_base_id": "kb_1", "document_id": "doc_1"},
    )
    await mcp.call_tool(
        "delete_knowledge_document",
        {"knowledge_base_id": "kb_1", "document_id": "doc_1"},
    )
    await mcp.call_tool(
        "finalize_knowledge_document",
        {"knowledge_base_id": "kb_1", "document_id": "doc_1"},
    )
    await mcp.call_tool("list_agent_evals", {"agent_id": "agent_1"})
    await mcp.call_tool("create_agent_eval", {"agent_id": "agent_1", "body": eval_body()})
    await mcp.call_tool("run_agent_eval", {"agent_id": "agent_1", "eval_id": "eval_1"})
    await mcp.call_tool("get_eval", {"eval_id": "eval_1"})
    await mcp.call_tool("inspect_workspace", {"workspace_root": str(tmp_path), "deep": False})
    await mcp.call_tool("build_session_config", {"body": {"prose": "A support agent"}})
    await mcp.call_tool("parse_external_config", {"format": "vapi", "raw": '{"name":"Demo"}'})
    await mcp.call_tool("render_briefing", {"agent_id": "agent_1"})
    share_result = await mcp.call_tool("create_share_card", {"build_id": "build_1"})

    paths = {(call["method"], call["path"]) for call in speko_api_mock}
    assert paths == EXPECTED_METHOD_PATHS
    assert {call["auth"] for call in speko_api_mock} == {"Bearer test-token"}
    assert share_result.structured_content["png_url"].endswith(".png")
    ledger = next(call for call in speko_api_mock if call["path"] == "/v1/credits/ledger")
    assert ledger["query"] == "limit=25&kind=grant%2Cdebit"
    available = next(
        call for call in speko_api_mock if call["path"] == "/v1/phone-numbers/available"
    )
    assert available["query"] == "areaCode=415&limit=2"


async def test_server_lists_exact_action_tools() -> None:
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == ACTION_TOOL_NAMES


EXPECTED_METHOD_PATHS = {
    ("GET", "/v1/organization"),
    ("GET", "/v1/credits/balance"),
    ("GET", "/v1/credits/ledger"),
    ("GET", "/v1/usage"),
    ("GET", "/v1/agents"),
    ("POST", "/v1/agents"),
    ("GET", "/v1/agents/agent_1"),
    ("PATCH", "/v1/agents/agent_1"),
    ("DELETE", "/v1/agents/agent_1"),
    ("GET", "/v1/agents/agent_1/tools"),
    ("POST", "/v1/agents/agent_1/tools"),
    ("GET", "/v1/agents/agent_1/tools/tool_1"),
    ("PATCH", "/v1/agents/agent_1/tools/tool_1"),
    ("DELETE", "/v1/agents/agent_1/tools/tool_1"),
    ("POST", "/v1/agents/agent_1/deploy"),
    ("POST", "/v1/agents/agent_1/rollback"),
    ("GET", "/v1/agents/agent_1/versions"),
    ("POST", "/v1/sessions"),
    ("POST", "/v1/sessions/phone"),
    ("GET", "/v1/sessions"),
    ("GET", "/v1/sessions/sess_1"),
    ("GET", "/v1/sessions/sess_1/transcript"),
    ("GET", "/v1/sessions/sess_1/recording"),
    ("GET", "/v1/agents/agent_1/calls"),
    ("GET", "/v1/calls/call_1"),
    ("GET", "/v1/calls/call_1/recording"),
    ("GET", "/v1/phone-numbers"),
    ("GET", "/v1/phone-numbers/available"),
    ("POST", "/v1/phone-numbers"),
    ("GET", "/v1/phone-numbers/pn_1"),
    ("PATCH", "/v1/phone-numbers/pn_1"),
    ("DELETE", "/v1/phone-numbers/pn_1"),
    ("POST", "/v1/knowledge-bases"),
    ("GET", "/v1/knowledge-bases"),
    ("GET", "/v1/knowledge-bases/kb_1"),
    ("DELETE", "/v1/knowledge-bases/kb_1"),
    ("GET", "/v1/knowledge-bases/kb_1/documents"),
    ("POST", "/v1/knowledge-bases/kb_1/documents"),
    ("GET", "/v1/knowledge-bases/kb_1/documents/doc_1"),
    ("DELETE", "/v1/knowledge-bases/kb_1/documents/doc_1"),
    ("POST", "/v1/knowledge-bases/kb_1/documents/doc_1/finalize"),
    ("GET", "/v1/agents/agent_1/evals"),
    ("POST", "/v1/agents/agent_1/evals"),
    ("POST", "/v1/agents/agent_1/evals/eval_1/run"),
    ("GET", "/v1/evals/eval_1"),
    ("POST", "/v1/inference/inspect"),
    ("POST", "/v1/inference/sessionconfig"),
    ("POST", "/v1/inference/parse-config"),
    ("POST", "/v1/inference/briefing"),
    ("POST", "/v1/share/build/build_1/card.png"),
}


def json_response(payload: Any, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def default_payload(path: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "path": path, "method": method, "body": body}


def agent_body() -> dict[str, object]:
    return {"name": "Demo", "systemPrompt": "Be helpful.", "intent": {"language": "en"}}


def tool_body() -> dict[str, object]:
    return {
        "name": "lookup",
        "description": "Look up data.",
        "parameters": {"type": "object"},
        "source": {"kind": "builtin", "name": "noop"},
    }


def session_config() -> dict[str, object]:
    return {"name": "Demo", "systemPrompt": "Be helpful.", "intent": {"language": "en"}}


def eval_body() -> dict[str, object]:
    return {"name": "Regression", "expected_behavior": "Say hello."}
