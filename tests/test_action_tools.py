from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import (
    ACTION_TOOL_NAMES,
    DISCLOSURE_OPENER,
    DISCLOSURE_RULE,
    PHONE_NUMBER_CONSENT_REQUIRED_CODE,
    PHONE_NUMBER_SCOPE_REQUIRED_CODE,
    next_step_for_error,
)
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import (
    DEFAULT_MANIFEST_ONLY_TOOL_NAMES,
    DEFAULT_PROFILE_ENV_VAR,
)
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
                "source": request.headers.get("x-speko-source"),
                "profile": request.headers.get("x-speko-mcp-profile"),
                "action_id": request.headers.get("x-speko-action-id"),
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
            return json_response(
                {
                    "sessionId": "sess_1",
                    "transportToken": "tok",
                    "transportUrl": "wss://transport.example",
                    "conversationToken": "tok",
                    "livekitUrl": "wss://transport.example",
                }
            )
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
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
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
) -> None:
    mcp = create_server()
    await mcp.call_tool("organization.get", {})
    await mcp.call_tool("credits.balance.get", {})
    await mcp.call_tool("credits.ledger.list", {"limit": 25, "kind": "grant,debit"})
    await mcp.call_tool("usage.summary.get", {"from_": "2026-05-01T00:00:00.000Z"})
    await mcp.call_tool("agents.list", {})
    await mcp.call_tool("agents.create", {"body": agent_body()})
    await mcp.call_tool("agents.get", {"agent_id": "agent_1"})
    await mcp.call_tool("agents.update", {"agent_id": "agent_1", "body": {"name": "Demo v2"}})
    await mcp.call_tool("agents.delete", {"agent_id": "agent_1"})
    await mcp.call_tool("agents.tools.list", {"agent_id": "agent_1"})
    await mcp.call_tool("agents.tools.create", {"agent_id": "agent_1", "body": tool_body()})
    await mcp.call_tool("agents.tools.get", {"agent_id": "agent_1", "tool_id": "tool_1"})
    await mcp.call_tool(
        "agents.tools.update",
        {"agent_id": "agent_1", "tool_id": "tool_1", "body": {"description": "Updated"}},
    )
    await mcp.call_tool("agents.tools.delete", {"agent_id": "agent_1", "tool_id": "tool_1"})
    await mcp.call_tool(
        "agents.deploy",
        {"agent_id": "agent_1", "session_config": session_config(), "source": "test"},
    )
    await mcp.call_tool("agents.rollback", {"agent_id": "agent_1", "target_version_number": 1})
    await mcp.call_tool("agents.versions.list", {"agent_id": "agent_1"})
    await mcp.call_tool(
        "agents.test_call",
        {"agent_id": "agent_1", "objective": "Ask the hours and book a table for 2."},
    )
    await mcp.call_tool("agents.test_call.get", {"agent_id": "agent_1", "run_id": "run_1"})
    await mcp.call_tool(
        "sessions.create", {"body": {"mode": "cascade", "intent": {"language": "en"}}}
    )
    await mcp.call_tool(
        "sessions.phone.create",
        {"body": {"to": "+12015550123", "intent": {"language": "en"}}},
    )
    await mcp.call_tool("sessions.list", {"limit": 10, "agent": "agent_1"})
    await mcp.call_tool("sessions.get", {"session_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"})
    await mcp.call_tool(
        "sessions.transcript.get", {"session_id": "22222222-2222-4222-8222-222222222222"}
    )
    await mcp.call_tool(
        "sessions.recording.get", {"session_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"}
    )
    await mcp.call_tool(
        "agents.calls.list", {"agent_id": "agent_1", "since": "2026-05-01T00:00:00.000Z"}
    )
    await mcp.call_tool("calls.get", {"call_id": "7f3e2a10-5b4c-4d8e-9a1f-2c3b4d5e6f70"})
    await mcp.call_tool("calls.recording.get", {"call_id": "7f3e2a10-5b4c-4d8e-9a1f-2c3b4d5e6f70"})
    await mcp.call_tool("phone_numbers.list", {})
    await mcp.call_tool("phone_numbers.available.search", {"area_code": "415", "limit": 2})
    await mcp.call_tool("phone_numbers.create", {"body": {"e164": "+12015550123"}})
    await mcp.call_tool(
        "phone_numbers.get", {"phone_number_id": "11111111-1111-4111-8111-111111111111"}
    )
    await mcp.call_tool(
        "phone_numbers.update", {"phone_number_id": "pn_1", "body": {"label": "Main"}}
    )
    await mcp.call_tool("phone_numbers.delete", {"phone_number_id": "pn_1"})
    await mcp.call_tool(
        "knowledge_bases.create", {"body": {"agentId": "agent_1", "name": "Default"}}
    )
    await mcp.call_tool("knowledge_bases.list", {"agent_id": "agent_1"})
    await mcp.call_tool("knowledge_bases.get", {"knowledge_base_id": "kb_1"})
    await mcp.call_tool("knowledge_bases.delete", {"knowledge_base_id": "kb_1"})
    await mcp.call_tool("knowledge_bases.documents.list", {"knowledge_base_id": "kb_1"})
    await mcp.call_tool(
        "knowledge_bases.documents.create",
        {
            "knowledge_base_id": "kb_1",
            "body": {"filename": "faq.md", "contentType": "text/markdown", "sizeBytes": 12},
        },
    )
    await mcp.call_tool(
        "knowledge_bases.documents.get",
        {"knowledge_base_id": "kb_1", "document_id": "doc_1"},
    )
    await mcp.call_tool(
        "knowledge_bases.documents.delete",
        {"knowledge_base_id": "kb_1", "document_id": "doc_1"},
    )
    await mcp.call_tool(
        "knowledge_bases.documents.finalize",
        {"knowledge_base_id": "kb_1", "document_id": "doc_1"},
    )
    await mcp.call_tool("agents.evals.list", {"agent_id": "agent_1"})
    await mcp.call_tool("agents.evals.create", {"agent_id": "agent_1", "body": eval_body()})
    await mcp.call_tool("agents.evals.run", {"agent_id": "agent_1", "eval_id": "eval_1"})
    await mcp.call_tool("evals.get", {"eval_id": "eval_1"})
    await mcp.call_tool(
        "migration.workspace.inspect",
        {"files": {"package.json": '{"name":"migration-fixture"}'}},
    )
    await mcp.call_tool("migration.session_config.build", {"body": {"prose": "A support agent"}})
    await mcp.call_tool(
        "migration.external_config.parse", {"format": "vapi", "raw": '{"name":"Demo"}'}
    )
    await mcp.call_tool("migration.briefing.render", {"agent_id": "agent_1"})
    share_result = await mcp.call_tool("share_cards.create", {"build_id": "build_1"})

    paths = {(call["method"], call["path"]) for call in speko_api_mock}
    assert paths == EXPECTED_METHOD_PATHS
    assert {call["auth"] for call in speko_api_mock} == {"Bearer sk_test-token"}
    assert {call["source"] for call in speko_api_mock} == {"mcp"}
    assert {call["profile"] for call in speko_api_mock} == {"default"}
    create = next(call for call in speko_api_mock if call["path"] == "/v1/agents")
    assert create["action_id"] == "agents.create"
    assert share_result.structured_content["png_url"].endswith(".png")
    ledger = next(
        call for call in speko_api_mock if call["path"] == "/v1/actions/credits.ledger.list"
    )
    assert ledger["body"] == {"limit": 25, "kind": "grant,debit"}
    available = next(
        call for call in speko_api_mock if call["path"] == "/v1/phone-numbers/available"
    )
    assert available["query"] == "areaCode=415&limit=2"


async def test_list_sessions_takes_agent_id_like_every_other_tool(
    speko_api_mock: list[dict[str, object]],
) -> None:
    mcp = create_server()
    await mcp.call_tool("sessions.list", {"agent_id": "agent_1"})
    await mcp.call_tool("sessions.list", {"agent": "agent_2"})

    queries = [call["query"] for call in speko_api_mock if call["path"] == "/v1/sessions"]
    assert queries == ["agent=agent_1", "agent=agent_2"]


async def test_server_lists_exact_action_tools() -> None:
    names = [tool.name for tool in await create_server().list_tools()]
    assert names == ACTION_TOOL_NAMES + DEFAULT_MANIFEST_ONLY_TOOL_NAMES + DOCS_TOOL_NAMES


async def test_create_agent_rejects_string_intent_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="body.intent must be an object"):
        await create_server().call_tool(
            "agents.create",
            {
                "body": {
                    "name": "Temp Migration Probe",
                    "systemPrompt": "You are a test agent.",
                    "intent": "customer_support",
                }
            },
        )

    assert speko_api_mock == []


async def test_create_session_requires_agent_or_intent_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="either agentId or intent is required"):
        await create_server().call_tool("sessions.create", {"body": {"mode": "cascade"}})

    assert speko_api_mock == []


async def test_create_session_rejects_string_intent_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="body.intent must be an object"):
        await create_server().call_tool("sessions.create", {"body": {"intent": "customer_support"}})

    assert speko_api_mock == []


async def test_create_session_s2s_pinned_provider_model_needs_no_agent_or_intent(
    speko_api_mock: list[dict[str, object]],
) -> None:
    # Mirrors createS2sSession in apps/server/src/routes/sessions.ts: an
    # explicit provider+model pin requires neither agentId nor intent.
    await create_server().call_tool(
        "sessions.create",
        {"body": {"mode": "s2s", "s2s": {"provider": "openai", "model": "gpt-realtime"}}},
    )

    assert [(call["method"], call["path"]) for call in speko_api_mock] == [("POST", "/v1/sessions")]


async def test_create_session_s2s_without_pin_requires_agent_or_intent(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="either agentId or intent is required"):
        await create_server().call_tool("sessions.create", {"body": {"mode": "s2s"}})

    assert speko_api_mock == []


async def test_create_session_s2s_rejects_provider_without_model_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="must be supplied together"):
        await create_server().call_tool(
            "sessions.create",
            {"body": {"mode": "s2s", "s2s": {"provider": "openai"}}},
        )

    assert speko_api_mock == []


async def test_create_session_s2s_pinned_still_validates_intent_shape(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="body.intent must be an object"):
        await create_server().call_tool(
            "sessions.create",
            {
                "body": {
                    "mode": "s2s",
                    "s2s": {"provider": "openai", "model": "gpt-realtime"},
                    "intent": "customer_support",
                }
            },
        )

    assert speko_api_mock == []


async def test_create_phone_session_rejects_non_e164_to_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="E.164"):
        await create_server().call_tool(
            "sessions.phone.create",
            {"body": {"to": "(201) 555-0123", "agentId": "agent_1"}},
        )

    assert speko_api_mock == []


async def test_call_tools_strip_the_sip_participant_prefix(
    speko_api_mock: list[dict[str, object]],
) -> None:
    session = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"

    await create_server().call_tool("calls.get", {"call_id": f"phone-{session}"})

    assert speko_api_mock[-1]["path"] == f"/v1/calls/{session}"


@pytest.mark.parametrize("bad_id", ["none", "open", "0b1c2d3e-4f50", "end_call"])
async def test_call_tools_reject_a_non_uuid_before_api(
    speko_api_mock: list[dict[str, object]], bad_id: str
) -> None:
    with pytest.raises(ToolError, match="sessionId"):
        await create_server().call_tool("calls.get", {"call_id": bad_id})
    with pytest.raises(ToolError, match="sessionId"):
        await create_server().call_tool("sessions.get", {"session_id": bad_id})

    assert speko_api_mock == []


async def test_connector_profile_discloses_on_the_real_phone_call_path(
    speko_api_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sessions.phone.create` is on the published directory surface (0.2.14).

    That the disclosure helper works is not the claim. The claim is that the
    body the directory's own call puts on the wire carries disclosure, in a
    caller-supplied first message the model chose.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")

    await create_server().call_tool(
        "sessions.phone.create",
        {
            "body": {
                "to": "+12015551234",
                "agentId": "agent_1",
                "firstMessage": "Hi, this is Ava from Northside Clinic.",
            }
        },
    )

    sent = [call for call in speko_api_mock if call["path"] == "/v1/sessions/phone"]
    assert len(sent) == 1, speko_api_mock
    body = sent[0]["body"]
    assert body["firstMessage"].startswith(DISCLOSURE_OPENER)
    assert DISCLOSURE_RULE in body["systemPrompt"]


@pytest.fixture
def session_api(monkeypatch: pytest.MonkeyPatch):
    """POST /v1/sessions plus GET /v1/agents/{id}[/versions], with each agent's
    runMode set per test (`agents[...] = "s2s"`) and its draft's session_config
    in `drafts[...]`; an unknown agent is a 404."""
    calls: list[dict[str, Any]] = []
    agents: dict[str, str] = {}
    drafts: dict[str, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}")
        calls.append({"method": request.method, "path": request.url.path, "body": body})
        if request.method == "GET" and request.url.path.startswith("/v1/agents/"):
            rest = request.url.path.removeprefix("/v1/agents/")
            agent_id, _, sub = rest.partition("/")
            if agent_id not in agents:
                return json_response({"error": "not found"}, status_code=404)
            if sub == "versions":
                live = {"status": "live", "session_config": {"runMode": agents[agent_id]}}
                if agent_id not in drafts:
                    return json_response([live])
                return json_response(
                    [{"status": "draft", "session_config": drafts[agent_id]}, live]
                )
            return json_response({"id": agent_id, "runMode": agents[agent_id]})
        return json_response({"sessionId": "sess_1"})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield SimpleNamespace(calls=calls, agents=agents, drafts=drafts)
    finally:
        http_client._TEST_TRANSPORT = None


def _sent_session(api: SimpleNamespace) -> dict[str, Any]:
    sent = [c for c in api.calls if c["method"] == "POST" and c["path"] == "/v1/sessions"]
    assert len(sent) == 1, api.calls
    return sent[0]["body"]


_GPT_LIVE = {"mode": "s2s", "s2s": {"provider": "openai", "model": "gpt-live-1"}}


async def test_directory_s2s_session_without_a_greeting_reaches_the_api_without_one(
    session_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The S2S body now honours `firstMessage`. Disclosure's fill of an absent
    one used to be dropped there, so it must not start replacing the greeting."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "muse")

    await create_server().call_tool("sessions.create", {"body": dict(_GPT_LIVE)})

    body = _sent_session(session_api)
    assert "firstMessage" not in body
    # The rest of the disclosure body is as it was.
    assert body["systemPrompt"] == DISCLOSURE_RULE
    assert not any(c["method"] == "GET" for c in session_api.calls)


@pytest.mark.parametrize(
    ("run_mode", "fabricated"),
    [("s2s", False), ("cascade", True), (None, True)],
)
async def test_directory_session_with_mode_omitted_reads_the_agents_run_mode(
    session_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    run_mode: str | None,
    fabricated: bool,
) -> None:
    """With no `mode` the server runs the agent's `runMode`. A GPT-Live agent's
    session keeps its stored greeting; a cascade agent's, and a failed lookup
    (None: the agent 404s), keep today's disclosure opener."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "muse")
    if run_mode is not None:
        session_api.agents["agent_1"] = run_mode

    await create_server().call_tool("sessions.create", {"body": {"agentId": "agent_1"}})

    body = _sent_session(session_api)
    if fabricated:
        assert body["firstMessage"] == DISCLOSURE_OPENER
    else:
        assert "firstMessage" not in body


@pytest.mark.parametrize(
    ("draft_flag", "deployed", "draft", "fabricated"),
    [
        # A draft that switches a cascade agent to GPT-Live runs as S2S.
        ("useDraft", "cascade", {"runMode": "s2s"}, False),
        # The reverse switch runs as cascade and keeps today's opener.
        ("use_draft", "s2s", {"runMode": "cascade"}, True),
        # A draft that leaves runMode alone keeps the deployed mode.
        ("useDraft", "s2s", {"firstMessage": "Draft hi"}, False),
        ("useDraft", "cascade", {"firstMessage": "Draft hi"}, True),
        # No draft at all: the deployed agent decides, as on the server.
        ("useDraft", "s2s", None, False),
    ],
)
async def test_directory_session_on_a_draft_reads_the_drafts_run_mode(
    session_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    draft_flag: str,
    deployed: str,
    draft: dict[str, Any] | None,
    fabricated: bool,
) -> None:
    """The server overlays the draft before it picks the mode, so the MCP
    must read the same mode or it fabricates (or drops) the wrong greeting."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "muse")
    session_api.agents["agent_1"] = deployed
    if draft is not None:
        session_api.drafts["agent_1"] = draft

    await create_server().call_tool(
        "sessions.create", {"body": {"agentId": "agent_1", draft_flag: True}}
    )

    body = _sent_session(session_api)
    if fabricated:
        assert body["firstMessage"] == DISCLOSURE_OPENER
    else:
        assert "firstMessage" not in body


@pytest.mark.parametrize(
    "body",
    [
        {**_GPT_LIVE, "firstMessage": "Hi, this is Ava."},
        {"mode": "cascade", "intent": {"language": "en"}},
    ],
)
async def test_directory_disclosure_is_unchanged_where_the_caller_chose(
    session_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """A greeting the caller sent keeps its disclosure prefix, and a cascade
    session keeps the opener fill, exactly as before."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "muse")

    await create_server().call_tool("sessions.create", {"body": dict(body)})

    sent = _sent_session(session_api)
    assert sent["firstMessage"].startswith(DISCLOSURE_OPENER)
    assert not any(c["method"] == "GET" for c in session_api.calls)


async def test_non_directory_session_body_passes_through_untouched(
    session_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)

    await create_server().call_tool("sessions.create", {"body": dict(_GPT_LIVE)})

    assert _sent_session(session_api) == _GPT_LIVE


async def test_update_agent_rejects_empty_body_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="at least one field"):
        await create_server().call_tool("agents.update", {"agent_id": "agent_1", "body": {}})

    assert speko_api_mock == []


@pytest.mark.parametrize(
    "partial",
    [{"name": "Front desk"}, {"voice": "alloy"}, {"intent": {"language": "en-US"}}],
)
async def test_muse_partial_agent_update_leaves_prompt_and_greeting_alone(
    speko_api_mock: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    partial: dict[str, object],
) -> None:
    """PATCH semantics: an unsent field is unchanged, not reset.

    Disclosure used to stamp `systemPrompt = DISCLOSURE_RULE` and
    `firstMessage = DISCLOSURE_OPENER` onto every directory-surface update, so
    a rename from Muse replaced the owner's whole prompt with one sentence.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "muse")

    await create_server().call_tool("agents.update", {"agent_id": "agent_1", "body": dict(partial)})

    sent = [call for call in speko_api_mock if call["path"] == "/v1/agents/agent_1"]
    assert len(sent) == 1, speko_api_mock
    assert sent[0]["method"] == "PATCH"
    assert sent[0]["body"] == partial


async def test_muse_agent_update_still_discloses_the_fields_it_sends(
    speko_api_mock: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "muse")

    await create_server().call_tool(
        "agents.update",
        {
            "agent_id": "agent_1",
            "body": {"systemPrompt": "You are Ava.", "firstMessage": "Hi, Ava here."},
        },
    )

    body = next(c for c in speko_api_mock if c["path"] == "/v1/agents/agent_1")["body"]
    assert body["systemPrompt"].startswith("You are Ava.")
    assert DISCLOSURE_RULE in body["systemPrompt"]
    assert body["firstMessage"].startswith(DISCLOSURE_OPENER)
    assert "Hi, Ava here." in body["firstMessage"]


async def test_create_agent_tool_rejects_missing_fields_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="missing required field"):
        await create_server().call_tool(
            "agents.tools.create",
            {"agent_id": "agent_1", "body": {"name": "lookup"}},
        )

    assert speko_api_mock == []


async def test_create_agent_tool_rejects_unknown_source_kind_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="source.kind must be one of"):
        await create_server().call_tool(
            "agents.tools.create",
            {
                "agent_id": "agent_1",
                "body": {
                    "name": "lookup",
                    "description": "Look up data.",
                    "parameters": {"type": "object"},
                    "source": {"kind": "lambda"},
                },
            },
        )

    assert speko_api_mock == []


async def test_create_agent_tool_webhook_requires_url_and_secret_before_api(
    speko_api_mock: list[dict[str, object]],
) -> None:
    with pytest.raises(ToolError, match="webhook source requires url and"):
        await create_server().call_tool(
            "agents.tools.create",
            {
                "agent_id": "agent_1",
                "body": {
                    "name": "lookup",
                    "description": "Look up data.",
                    "parameters": {"type": "object"},
                    "source": {"kind": "webhook", "url": "https://example.com/hook"},
                },
            },
        )

    assert speko_api_mock == []


async def test_agent_tool_simulation_passes_through_create_and_update(
    speko_api_mock: list[dict[str, object]],
) -> None:
    mcp = create_server()
    simulation = {"mode": "mock", "response": {"question": "What days can you start?"}}
    await mcp.call_tool(
        "agents.tools.create",
        {"agent_id": "agent_1", "body": {**tool_body(), "simulation": simulation}},
    )
    await mcp.call_tool(
        "agents.tools.update",
        {"agent_id": "agent_1", "tool_id": "tool_1", "body": {"simulation": {"mode": "live"}}},
    )
    await mcp.call_tool(
        "agents.tools.update",
        {"agent_id": "agent_1", "tool_id": "tool_1", "body": {"simulation": None}},
    )

    writes = [c for c in speko_api_mock if str(c["path"]).startswith("/v1/agents/agent_1/tools")]
    assert [c["method"] for c in writes] == ["POST", "PATCH", "PATCH"]
    assert writes[0]["body"]["simulation"] == simulation  # type: ignore[index]
    assert writes[1]["body"] == {"simulation": {"mode": "live"}}
    assert writes[2]["body"] == {"simulation": None}


@pytest.mark.parametrize(
    ("tool", "simulation", "match"),
    [
        ("agents.tools.create", None, "simulation cannot be null"),
        ("agents.tools.create", {"mode": "skip"}, "simulation.mode must be one of"),
        ("agents.tools.create", "mock", "simulation.mode must be one of"),
        ("agents.tools.update", {"mode": "live", "response": "x"}, "does not accept response"),
        ("agents.tools.update", {"mode": "mock", "respone": {}}, "does not accept respone"),
        ("agents.tools.update", {"mode": "mock", "response": "x" * 8_200}, "at most 8192"),
        # 3,000 emoji: 3,000 code points but 12,000 UTF-8 bytes on the wire.
        ("agents.tools.update", {"mode": "mock", "response": "\U0001f600" * 3_000}, "at most 8192"),
    ],
)
async def test_agent_tool_simulation_rejects_bad_shapes_before_api(
    speko_api_mock: list[dict[str, object]],
    tool: str,
    simulation: object,
    match: str,
) -> None:
    body: dict[str, object] = {"simulation": simulation}
    args: dict[str, object] = {"agent_id": "agent_1", "body": body}
    if tool == "agents.tools.create":
        body.update(tool_body())
    else:
        args["tool_id"] = "tool_1"
    with pytest.raises(ToolError, match=match):
        await create_server().call_tool(tool, args)

    assert speko_api_mock == []


async def test_agent_tool_write_descriptions_document_simulation() -> None:
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name in ("agents.tools.create", "agents.tools.update"):
        body_doc = tools[name].parameters["properties"]["body"]["description"]
        assert "simulation?:" in body_doc, name
        assert "{mode:'live'}" in body_doc, name
        assert "outputBindings" in body_doc, name
    assert (
        "null to return the tool to the default policy"
        in (tools["agents.tools.update"].parameters["properties"]["body"]["description"])
    )
    assert "simulation" in (tools["agents.tools.get"].description or "")


def test_error_details_include_validation_issues() -> None:
    response = httpx.Response(
        400,
        json={
            "error": "Invalid request",
            "code": "VALIDATION_ERROR",
            "issues": [
                {"path": "intent", "message": "Expected object, received string"},
                {"path": "systemPrompt", "message": "Required"},
            ],
        },
        headers={"x-request-id": "req_123"},
    )

    message, trace_id = http_client._error_details(response)

    assert trace_id == "req_123"
    assert message == (
        "Invalid request: intent: Expected object, received string; systemPrompt: Required"
    )


def test_phone_scope_and_consent_403_map_to_distinct_next_steps() -> None:
    # These two 403s used to share one code and fall into the generic
    # "check authentication and retry" branch, giving the agent no way to tell
    # a dead-end (must reconnect) from a fixable state (must accept consent).
    scope_missing = http_client.SpekoApiError(
        403,
        "Reconnect required",
        trace_id="t1",
        code=PHONE_NUMBER_SCOPE_REQUIRED_CODE,
    )
    consent_missing = http_client.SpekoApiError(
        403,
        "Consent required",
        trace_id="t2",
        code=PHONE_NUMBER_CONSENT_REQUIRED_CODE,
    )

    scope_step = next_step_for_error(scope_missing, path="/v1/sessions/phone")
    consent_step = next_step_for_error(consent_missing, path="/v1/sessions/phone")

    assert scope_step != consent_step
    assert scope_step != "Check authentication and retry the Speko MCP request."
    assert consent_step != "Check authentication and retry the Speko MCP request."
    assert "reconnect" in scope_step.lower()
    assert "consent" in consent_step.lower()


def _error_response(payload: dict[str, Any], *, status_code: int = 403) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://api.speko.dev/v1/sessions/phone"),
    )


@pytest.mark.parametrize(
    "reconnect_instructions",
    [
        pytest.param(
            "Reconnect the Speko connector in your client so it authorizes again - "
            "a token refresh will not add this permission. Phone use must also be "
            "accepted for this workspace at "
            "https://platform.speko.ai/settings/phone-authorization.",
            id="generic-client",
        ),
        pytest.param(
            "Reconnect Speko at https://claude.ai/directory/speko and approve phone "
            "calling, then retry - a token refresh will not add this permission.",
            id="claude-directory",
        ),
        pytest.param(
            "Reconnect Speko at "
            "https://chatgpt.com/plugins/plugin_asdk_app_6a88aa7070e88191b5825453492c5cf5"
            "?open_in_app and approve phone calling, then retry - "
            "a token refresh will not add this permission.",
            id="chatgpt-directory",
        ),
    ],
)
def test_phone_scope_recovery_preserves_the_server_reconnect_location(
    reconnect_instructions: str,
) -> None:
    # The generic URL is a later workspace-consent step, not a way to widen
    # OAuth scopes. Render the same error path the tool returns to its caller.
    server_message = (
        f"This connection is not authorized for phone calling. {reconnect_instructions}"
    )
    response = _error_response({"error": server_message, "code": PHONE_NUMBER_SCOPE_REQUIRED_CODE})
    response.headers["x-request-id"] = "trace-phone-scope"
    with pytest.raises(http_client.SpekoApiError) as error:
        http_client._raise_api_error(response)

    next_step = next_step_for_error(error.value, path="/v1/sessions/phone")
    rendered = http_client.tool_error_message(error.value, next_step=next_step)

    assert server_message in rendered
    assert "trace_id=trace-phone-scope" in rendered
    assert "follow the reconnect instructions above" in next_step
    assert "Workspace consent and token refresh cannot add this permission" in next_step
    assert "Retry only after they confirm a new authorization" in next_step
    assert "the page that can re-authorize it" not in rendered


def test_phone_consent_recovery_keeps_the_workspace_link_without_reconnecting() -> None:
    server_message = (
        "This workspace has not accepted the phone-use authorization yet. Open "
        "https://platform.speko.ai/settings/phone-authorization, accept it, then retry the call."
    )
    response = _error_response(
        {"error": server_message, "code": PHONE_NUMBER_CONSENT_REQUIRED_CODE}
    )
    with pytest.raises(http_client.SpekoApiError) as error:
        http_client._raise_api_error(response)

    next_step = next_step_for_error(error.value, path="/v1/sessions/phone")
    rendered = http_client.tool_error_message(error.value, next_step=next_step)

    assert server_message in rendered
    assert "accept the phone-use consent for this workspace" in next_step
    assert "No reconnect is needed" in next_step
    assert "new authorization" not in next_step


@pytest.mark.parametrize(
    ("profile", "names_purchase_tools"),
    [
        (None, True),
        ("customer", True),
        ("chatgpt", False),
        ("replit", False),
        ("connector", False),
    ],
)
def test_failed_phone_provisioning_says_buy_a_number_not_retry(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, names_purchase_tools: bool
) -> None:
    # Platform replays a failed automatic purchase on every later call without
    # contacting the carrier, so "Retry" produced four identical 502s in 5 min.
    # Profiles that serve calling without the purchase tools get the page only.
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    response = _error_response(
        {
            "error": "The phone provider rejected the number order.",
            "code": "PHONE_NUMBER_PROVISIONING_FAILED",
            "retryable": False,
        },
        status_code=502,
    )
    with pytest.raises(http_client.SpekoApiError) as error:
        http_client._raise_api_error(response)

    next_step = next_step_for_error(error.value, path="/v1/sessions/phone")

    assert "Phone numbers page" in next_step
    assert ("phone_numbers.create" in next_step) is names_purchase_tools
    assert "Retry the Speko MCP request" not in next_step


def test_platform_hint_reaches_the_model() -> None:
    # Platform attaches `hint` to every coded error from the curated registry,
    # and the parser used to read only `error`/`code` - so the one field written
    # to tell a caller what to DO never reached an MCP user on any error.
    details = http_client._parse_api_error(
        _error_response(
            {
                "error": "This connection is not authorized for phone calling.",
                "code": "PHONE_NUMBER_SCOPE_REQUIRED",
                "hint": "Re-authorize the connector at the page named above.",
            }
        )
    )

    assert details.code == "PHONE_NUMBER_SCOPE_REQUIRED"
    assert "Re-authorize the connector" in details.message


def test_a_hint_the_message_already_states_is_not_repeated() -> None:
    # The phone 403s name their URL inline, so a hint restating it would read
    # the remedy to the user twice in one sentence.
    hint = "Open https://platform.speko.ai/settings/phone-authorization to accept."
    details = http_client._parse_api_error(
        _error_response({"error": f"Not accepted yet. {hint}", "code": "X", "hint": hint})
    )

    assert details.message.count("settings/phone-authorization") == 1


def test_an_error_without_a_hint_is_unchanged() -> None:
    details = http_client._parse_api_error(_error_response({"error": "Nope.", "code": "X"}))

    assert details.message == "Nope."


def test_action_field_issues_reach_the_model() -> None:
    # `/v1/actions/*` nests its per-field reasons under `error.fieldIssues`, and
    # the parser returned as soon as it had `error.message` -- so a rejected
    # `agents.graph.replace` said only that validation failed and never which
    # node or edge, leaving the caller to guess and retry blind.
    details = http_client._parse_api_error(
        _error_response(
            {
                "error": {
                    "code": "INVALID_GRAPH",
                    "message": "The graph failed structural validation.",
                    "fieldIssues": [
                        {
                            "path": "edges.2",
                            "code": "invalid_graph",
                            "message": "a tool node may only carry tool_result edges",
                        },
                        {
                            "path": "nodes.4.slots",
                            "code": "invalid_graph",
                            "message": "question is referenced but never declared",
                        },
                    ],
                    "retryable": False,
                    "requestId": "req_9",
                    "nextAction": "Fix the listed nodes/edges and call agents.graph.replace again.",
                }
            }
        )
    )

    assert details.code == "INVALID_GRAPH"
    assert "edges.2: a tool node may only carry tool_result edges" in details.message
    assert "nodes.4.slots: question is referenced but never declared" in details.message
    assert "call agents.graph.replace again" in details.message
    assert details.trace_id == "req_9"


def test_an_action_error_without_field_issues_is_unchanged() -> None:
    details = http_client._parse_api_error(
        _error_response(
            {"error": {"code": "AGENT_NOT_FOUND", "message": "No such agent.", "fieldIssues": []}}
        )
    )

    assert details.message == "AGENT_NOT_FOUND: No such agent."


def test_a_next_action_the_message_already_states_is_not_repeated() -> None:
    step = "Call agents.versions.list to see the versions that exist."
    details = http_client._parse_api_error(
        _error_response(
            {
                "error": {
                    "code": "AGENT_VERSION_NOT_FOUND",
                    "message": f"No such version. {step}",
                    "nextAction": step,
                }
            }
        )
    )

    assert details.message.count("agents.versions.list") == 1


def test_unrecognized_403_falls_back_to_generic_auth_message() -> None:
    other = http_client.SpekoApiError(403, "Forbidden", trace_id="t3", code="SOME_OTHER_CODE")

    assert (
        next_step_for_error(other, path="/v1/sessions/phone")
        == "Check authentication and retry the Speko MCP request."
    )


EXPECTED_METHOD_PATHS = {
    ("POST", "/v1/actions/organization.get"),
    ("POST", "/v1/actions/credits.balance.get"),
    ("POST", "/v1/actions/credits.ledger.list"),
    ("POST", "/v1/actions/usage.summary.get"),
    ("POST", "/v1/actions/agents.list"),
    ("POST", "/v1/agents"),
    ("POST", "/v1/actions/agents.get"),
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
    ("POST", "/v1/agents/agent_1/test-call"),
    ("GET", "/v1/agents/agent_1/eval-runs/run_1"),
    ("POST", "/v1/sessions"),
    ("POST", "/v1/sessions/phone"),
    ("GET", "/v1/sessions"),
    ("GET", "/v1/sessions/1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"),
    ("POST", "/v1/actions/sessions.transcript.get"),
    ("GET", "/v1/sessions/1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d/recording"),
    ("GET", "/v1/agents/agent_1/calls"),
    ("GET", "/v1/calls/7f3e2a10-5b4c-4d8e-9a1f-2c3b4d5e6f70"),
    ("GET", "/v1/calls/7f3e2a10-5b4c-4d8e-9a1f-2c3b4d5e6f70/recording"),
    ("POST", "/v1/actions/phone_numbers.list"),
    ("GET", "/v1/phone-numbers/available"),
    ("POST", "/v1/phone-numbers"),
    ("POST", "/v1/actions/phone_numbers.get"),
    ("PATCH", "/v1/phone-numbers/pn_1"),
    ("DELETE", "/v1/phone-numbers/pn_1"),
    ("POST", "/v1/knowledge-bases"),
    ("POST", "/v1/actions/knowledge_bases.list"),
    ("POST", "/v1/actions/knowledge_bases.get"),
    ("DELETE", "/v1/knowledge-bases/kb_1"),
    ("POST", "/v1/actions/knowledge_bases.documents.list"),
    ("POST", "/v1/knowledge-bases/kb_1/documents"),
    ("POST", "/v1/actions/knowledge_bases.documents.get"),
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


# --- bring your own carrier --------------------------------------------------

SIP_PASSWORD = "odorik-digest-secret"
SIP_CONNECTION_ID = "5b0f6f7e-3c1d-4e2a-9b8c-7d6e5f4a3b21"
BYO_CARRIER_TOOLS = ("phone_numbers.sip_connections.create", "phone_numbers.import")


@pytest.fixture
def byo_carrier_api(monkeypatch: pytest.MonkeyPatch):
    """Platform's action endpoint as the two BYO-carrier actions answer it."""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8") or "{}")
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "idempotency_key": request.headers.get("idempotency-key"),
                "body": body,
            }
        )
        if request.url.path.endswith("/phone_numbers.sip_gateway.get"):
            return json_response(
                {
                    "error": {
                        "code": "SIP_GATEWAY_DISABLED",
                        "message": "SIP gateway access is not enabled for this organization.",
                        "retryable": False,
                        "nextAction": (
                            "To bring your own carrier, SIP trunk or number, no gateway is "
                            "needed: call phone_numbers.sip_connections.create, then "
                            "phone_numbers.import."
                        ),
                    }
                },
                status_code=403,
            )
        if request.url.path.endswith("/phone_numbers.sip_connections.create"):
            # The real summary: whether a password is set, never the password.
            return json_response(
                {
                    "kind": "result",
                    "data": {
                        "id": SIP_CONNECTION_ID,
                        "status": "connected",
                        "display_name": "Odorik",
                        "provider": "generic",
                        "provider_label": "Odorik",
                        "host": "sip.odorik.cz",
                        "port": 5060,
                        "transport": "udp",
                        "allow_inbound": True,
                        "allow_outbound": True,
                        "has_auth_username": True,
                        "has_auth_password": True,
                        "created_at": "2026-10-16T09:00:00.000Z",
                    },
                    "warnings": [],
                    "resourceLinks": [],
                }
            )
        return json_response({"kind": "result", "data": {"e164": body.get("e164")}})

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "customer")
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        http_client._TEST_TRANSPORT = None


async def test_sip_connection_create_posts_the_action_and_never_echoes_the_password(
    byo_carrier_api: list[dict[str, Any]],
) -> None:
    result = await create_server().call_tool(
        "phone_numbers.sip_connections.create",
        {
            "host": "sip.odorik.cz",
            "allow_inbound": True,
            "allow_outbound": True,
            "auth_username": "1234567",
            "auth_password": SIP_PASSWORD,
            "provider_label": "Odorik",
        },
    )

    [call] = byo_carrier_api
    assert call["method"] == "POST"
    assert call["path"] == "/v1/actions/phone_numbers.sip_connections.create"
    assert call["body"]["auth_password"] == SIP_PASSWORD
    # Each call inserts a connection, so a retry must carry a replay key.
    assert call["idempotency_key"] and call["idempotency_key"].startswith("mcp-")
    rendered = json.dumps(result.structured_content) + "".join(
        block.text for block in result.content if hasattr(block, "text")
    )
    assert SIP_PASSWORD not in rendered
    assert result.structured_content["data"]["has_auth_password"] is True


async def test_phone_number_import_posts_the_contract_body(
    byo_carrier_api: list[dict[str, Any]],
) -> None:
    await create_server().call_tool(
        "phone_numbers.import",
        {
            "e164": "+420212345678",
            "sip_connection_installation_id": SIP_CONNECTION_ID,
            "direction": "both",
            "agent_id": "agent_1",
        },
    )

    [call] = byo_carrier_api
    assert call["path"] == "/v1/actions/phone_numbers.import"
    assert call["body"] == {
        "e164": "+420212345678",
        "sip_connection_installation_id": SIP_CONNECTION_ID,
        "direction": "both",
        "agent_id": "agent_1",
    }


def test_sip_connection_password_is_a_declared_secret() -> None:
    from spekoai_mcp.action_manifest import action_entries

    entries = {entry["id"]: entry for entry in action_entries()}
    assert entries["phone_numbers.sip_connections.create"]["secretInputPaths"] == ["auth_password"]
    output = json.dumps(entries["phone_numbers.sip_connections.create"]["outputSchema"])
    assert '"auth_password"' not in output


@pytest.mark.parametrize(
    ("profile", "served"),
    [
        ("customer", True),
        (None, False),
        ("muse", False),
        ("connector", False),
        ("chatgpt", False),
        ("replit", False),
        ("builder", False),
    ],
)
async def test_byo_carrier_tools_are_served_where_phone_numbers_are_managed(
    monkeypatch: pytest.MonkeyPatch, profile: str | None, served: bool
) -> None:
    if profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, profile)
    names = {tool.name for tool in await create_server().list_tools()}
    assert all((name in names) is served for name in BYO_CARRIER_TOOLS)


async def test_sip_gateway_disabled_points_at_import_not_a_ticket(
    byo_carrier_api: list[dict[str, Any]],
) -> None:
    with pytest.raises(ToolError) as error:
        await create_server().call_tool("phone_numbers.sip_gateway.get", {})

    message = str(error.value)
    assert "SIP_GATEWAY_DISABLED" in message
    assert "phone_numbers.import" in message
    # Nothing in it says "contact support", so no ticket step is appended.
    assert "support=" not in message
