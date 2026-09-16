"""Per-invocation provenance, independent from authorization and tool payloads."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID

import httpx
import mcp.types as mt
import pytest
from fastmcp import Client
from fastmcp.exceptions import NotFoundError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult
from starlette.testclient import TestClient

from spekoai_mcp import attribution, http_client
from spekoai_mcp.attribution import (
    InvocationAttributionMiddleware,
    invocation_headers,
    observed_host,
    reset_current_host,
    set_current_host,
)
from spekoai_mcp.attribution_export import BoundedLogExporter
from spekoai_mcp.profiles import DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import (
    MCP_PROTOCOL_VERSION,
    MCPProtocolGuard,
    create_app,
    create_public_server,
    create_server,
)


@pytest.fixture(autouse=True)
def attribution_enabled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[None]:
    monkeypatch.setenv("ATTRIBUTION_ENABLED", "true")
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    caplog.set_level(logging.INFO, logger=attribution.__name__)

    def capture_event(event):
        logging.getLogger(attribution.__name__).info(json.dumps(event), extra=event)

    exporter = BoundedLogExporter(capture_event)
    monkeypatch.setattr(attribution, "_EXPORTER", exporter)
    yield
    assert exporter.wait_until_idle(2)
    exporter.close(timeout=1)


def _context(name: str = "agents.list") -> MiddlewareContext[mt.CallToolRequestParams]:
    return MiddlewareContext(
        message=mt.CallToolRequestParams(name=name, arguments={"private": "DO NOT LOG"}),
        method="tools/call",
    )


def _events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    assert attribution._EXPORTER.wait_until_idle(2)
    return [
        json.loads(record.message)
        for record in caplog.records
        if record.name == attribution.__name__
    ]


@pytest.fixture
def relay(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handle))
    monkeypatch.setattr(http_client, "_bearer_token", lambda: "sk_unchanged_platform_credential")
    return requests


async def test_repeated_tools_get_distinct_ids_and_each_fanout_shares_one(
    relay: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def call_next(_):
        action_token = http_client.set_current_action_id("agents.list")
        try:
            await asyncio.gather(
                http_client.call_speko_api("GET", "/v1/agents"),
                http_client.call_speko_api_any("GET", "/v1/sessions"),
                http_client.call_speko_api_raw("GET", "/v1/raw"),
                http_client.post_speko_api_bytes(
                    "/v1/transcribe", b"audio", content_type="audio/wav"
                ),
                http_client.call_action("agents.list", {}, idempotency_key="idempotency-unchanged"),
            )
            return ToolResult(content="private response")
        finally:
            http_client.reset_current_action_id(action_token)

    middleware = InvocationAttributionMiddleware()
    host_token = set_current_host("mcp.speko.ai")
    try:
        await middleware.on_call_tool(_context(), call_next)
        await middleware.on_call_tool(_context(), call_next)
    finally:
        reset_current_host(host_token)

    first_ids = {request.headers["x-speko-invocation-id"] for request in relay[:5]}
    second_ids = {request.headers["x-speko-invocation-id"] for request in relay[5:]}
    assert len(first_ids) == len(second_ids) == 1
    assert first_ids != second_ids
    assert all(UUID(value).version == 4 for value in first_ids | second_ids)
    assert all(request.headers["x-speko-action-id"] == "agents.list" for request in relay)
    assert all(request.headers["x-speko-mcp-host"] == "mcp.speko.ai" for request in relay)
    assert all(request.headers["x-speko-source"] == "mcp" for request in relay)
    assert all(request.headers["x-speko-client"] == "unknown-mcp-client" for request in relay)
    assert all(
        request.headers["authorization"] == "Bearer sk_unchanged_platform_credential"
        for request in relay
    )
    assert relay[4].headers["idempotency-key"] == "idempotency-unchanged"
    assert invocation_headers() == {}
    events = _events(caplog)
    assert len(events) == 2
    assert {event["invocation_id"] for event in events} == first_ids | second_ids
    assert all(event["receivers"] == ["platform"] for event in events)
    assert all(event["outcome"] == "succeeded" for event in events)
    assert "private response" not in caplog.text
    assert "DO NOT LOG" not in caplog.text
    assert "sk_unchanged_platform_credential" not in caplog.text


async def test_concurrent_invocations_keep_ids_and_hosts_separate(
    relay: list[httpx.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def invoke(host: str):
        host_token = set_current_host(host)
        try:

            async def call_next(_):
                before = invocation_headers()
                await http_client.call_speko_api("GET", "/v1/agents")
                await asyncio.sleep(0)
                assert invocation_headers() == before
                return ToolResult(content="ok")

            await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
            assert "X-Speko-Invocation-Id" not in invocation_headers()
        finally:
            reset_current_host(host_token)

    await asyncio.gather(invoke("chatgpt.speko.ai"), invoke("mcp.speko.ai"))
    assert len({request.headers["x-speko-invocation-id"] for request in relay}) == 2
    pairs = {
        (request.headers["x-speko-invocation-id"], request.headers["x-speko-mcp-host"])
        for request in relay
    }
    assert pairs == {(event["invocation_id"], event["host"]) for event in _events(caplog)}
    assert invocation_headers() == {}


@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE FAILURE"), asyncio.CancelledError()])
async def test_error_and_cancellation_emit_once_and_reset(failure, caplog) -> None:
    async def call_next(_):
        assert "X-Speko-Invocation-Id" in invocation_headers()
        raise failure

    with pytest.raises(type(failure)) as raised:
        await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    assert raised.value is failure
    assert invocation_headers() == {}
    events = _events(caplog)
    assert len(events) == 1
    assert events[0]["outcome"] == (
        "cancelled" if isinstance(failure, asyncio.CancelledError) else "failed"
    )
    assert events[0]["receivers"] == ["local"]
    assert "PRIVATE FAILURE" not in caplog.text


async def test_returned_tool_error_counts_as_failed(caplog) -> None:
    result = ToolResult(content="private tool error", is_error=True)

    async def call_next(_):
        return result

    assert await InvocationAttributionMiddleware().on_call_tool(_context(), call_next) is result
    assert _events(caplog)[0]["outcome"] == "failed"
    assert "private tool error" not in caplog.text


async def test_router_direct_and_platform_receivers_survive_fanout(relay, caplog) -> None:
    async def call_next(_):
        await asyncio.gather(
            http_client.post_router_speech({"text": "private audio text"}, token="sk_router_key"),
            http_client.post_router_transcription(
                b"private audio", request_payload={}, token="sk_router_key"
            ),
            http_client.call_speko_api("GET", "/v1/agents"),
        )
        return ToolResult(content="ok")

    await InvocationAttributionMiddleware().on_call_tool(_context("audio.synthesize"), call_next)
    assert _events(caplog)[0]["receivers"] == ["platform", "router_direct"]
    assert len({request.headers["x-speko-invocation-id"] for request in relay}) == 1
    assert all(request.headers["authorization"] == "Bearer sk_router_key" for request in relay[:2])
    assert relay[0].headers["user-agent"] == relay[1].headers["user-agent"] == "spekoai-mcp"
    assert relay[0].headers["idempotency-key"] != relay[1].headers["idempotency-key"]
    assert "sk_router_key" not in caplog.text
    assert "private audio" not in caplog.text


async def test_router_network_failure_keeps_receiver(monkeypatch, caplog) -> None:
    async def handle(request):
        raise httpx.ConnectError("private upstream error", request=request)

    monkeypatch.setattr(http_client, "_TEST_TRANSPORT", httpx.MockTransport(handle))

    async def call_next(_):
        await http_client.post_router_speech({}, token="sk_router_key")
        return ToolResult(content="unreachable")

    with pytest.raises(http_client.SpekoApiError):
        await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    assert _events(caplog)[0]["receivers"] == ["router_direct"]
    assert _events(caplog)[0]["outcome"] == "failed"
    assert "private upstream error" not in caplog.text


async def test_missing_credentials_do_not_claim_platform_request(monkeypatch, caplog) -> None:
    monkeypatch.setattr(http_client, "get_access_token", lambda: None)

    async def call_next(_):
        await http_client.call_speko_api("GET", "/v1/agents")
        return ToolResult(content="unreachable")

    with pytest.raises(http_client.SpekoAuthError):
        await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    assert _events(caplog)[0]["receivers"] == ["local"]


async def test_disabled_by_default_but_ids_still_propagate(monkeypatch, relay, caplog) -> None:
    monkeypatch.delenv("ATTRIBUTION_ENABLED")

    async def call_next(_):
        await http_client.call_speko_api("GET", "/v1/agents")
        return ToolResult(content="ok")

    await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    assert UUID(relay[0].headers["x-speko-invocation-id"]).version == 4
    assert not _events(caplog)


async def test_export_failure_does_not_change_result_or_context(monkeypatch) -> None:
    def broken_log(*args, **kwargs):
        raise OSError("log sink unavailable")

    monkeypatch.setattr(attribution._EXPORTER, "_write", broken_log)
    result = ToolResult(content="ok")

    async def call_next(_):
        return result

    assert await InvocationAttributionMiddleware().on_call_tool(_context(), call_next) is result
    assert invocation_headers() == {}
    assert attribution._EXPORTER.wait_until_idle(2)
    assert attribution._EXPORTER.snapshot()["drop_counts"]["sink_error"] == 1


async def test_allowlisted_verified_principal_only(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        attribution,
        "get_access_token",
        lambda: SimpleNamespace(
            token="secret bearer token",
            client_id="oauth-client-1",
            claims={
                "sub": "user-1",
                "organization_id": "org-1",
                "email": "private@example.com",
                "name": "Private Name",
                "arbitrary": "PRIVATE DATA",
            },
        ),
    )

    async def call_next(_):
        return ToolResult(content="ok")

    await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    event = _events(caplog)[0]
    assert event["auth_kind"] == "oauth_user"
    assert event["user_id"] == "user-1"
    assert event["organization_id"] == "org-1"
    assert event["oauth_client_id"] == "oauth-client-1"
    assert event["api_key_id"] is None
    assert all(
        private not in caplog.text
        for private in [
            "secret bearer token",
            "private@example.com",
            "Private Name",
            "PRIVATE DATA",
        ]
    )


@pytest.mark.parametrize(
    "credential", ["sk_synthetic_private", "sk-synthetic-private", "eyJprivate"]
)
async def test_credentials_never_become_exported_identifiers_or_user_agent(
    credential,
    monkeypatch,
    caplog,
    relay,
) -> None:
    monkeypatch.setattr(
        attribution,
        "get_access_token",
        lambda: SimpleNamespace(
            client_id=credential,
            subject=credential,
            claims={"organization_id": credential},
        ),
    )
    user_agent = f"Cursor/1 token={credential} email=private@example.com"
    ua_token = http_client.set_current_client_ua(user_agent)
    try:

        async def call_next(_):
            await http_client.call_speko_api("GET", "/v1/agents")
            return ToolResult(content="ok")

        await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    finally:
        http_client.reset_current_client_ua(ua_token)
    event = _events(caplog)[0]
    assert event["event_version"] == 3
    assert event["execution_client"] == "cursor"
    assert event["client_evidence_class"] == "observed_client_marker"
    assert event["client_id"] is event["oauth_client_id"] is event["user_id"] is None
    assert event["organization_id"] is None
    assert "client_ua" not in event
    assert credential not in json.dumps(event)
    assert "private@example.com" not in json.dumps(event)
    # This pre-existing internal protocol remains independent from log exports.
    assert relay[0].headers["x-speko-client-ua"] == user_agent


async def test_detached_child_cannot_reuse_completed_invocation() -> None:
    completed = asyncio.Event()
    child = None

    async def call_next(_):
        nonlocal child

        async def after_completion():
            await completed.wait()
            return invocation_headers()

        child = asyncio.create_task(after_completion())
        return ToolResult(content="ok")

    await InvocationAttributionMiddleware().on_call_tool(_context(), call_next)
    completed.set()
    assert child is not None
    assert "X-Speko-Invocation-Id" not in await child


async def test_real_middleware_covers_public_local_tool_and_excludes_tool_listing(caplog) -> None:
    async with Client(create_public_server()) as client:
        await client.list_tools()
        assert not _events(caplog)
        await client.call_tool("docs.search", {"query": "quickstart"})
    events = _events(caplog)
    assert len(events) == 1
    assert events[0]["tool"] == "docs.search"
    assert events[0]["profile"] == "public"
    assert events[0]["receivers"] == ["local"]
    assert events[0]["outcome"] == "succeeded"


async def test_real_middleware_records_profile_refusal(monkeypatch, caplog) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "builder")
    server = create_server()
    with pytest.raises(NotFoundError):
        await server.call_tool("sessions.list", {})
    events = _events(caplog)
    assert len(events) == 1
    assert events[0]["tool"] == "sessions.list"
    assert events[0]["outcome"] == "failed"
    assert invocation_headers() == {}


def test_host_and_verified_principal_survive_real_http_dispatch(relay, caplog) -> None:
    class VerifiedApiKey(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            return AccessToken(
                token=token,
                client_id="api-key:key-1",
                scopes=[],
                claims={
                    "auth_method": "api_key",
                    "api_key_id": "key-1",
                    "organization_id": "org-1",
                },
            )

    with TestClient(create_app(auth=VerifiedApiKey())) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer sk_unchanged_platform_credential",
                "Host": "mcp.speko.ai",
                "User-Agent": "test-harness/1",
                "X-Speko-MCP-Host": "forged.example",
                "X-Speko-Invocation-Id": "00000000-0000-4000-8000-000000000000",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": "agents.list",
                "Accept": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "agents.list",
                    "arguments": {},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                    },
                },
            },
        )

    assert response.status_code == 200, response.text
    assert not response.json()["result"].get("isError", False)
    events = _events(caplog)
    assert len(events) == len(relay) == 1
    event = events[0]
    assert event["auth_kind"] == "api_key"
    assert event["api_key_id"] == "key-1"
    assert event["organization_id"] == "org-1"
    assert event["oauth_client_id"] is None
    assert event["host"] == relay[0].headers["x-speko-mcp-host"] == "mcp.speko.ai"
    assert relay[0].headers["x-speko-client-ua"] == "test-harness/1"
    assert "client_ua" not in event
    assert event["execution_client"] == "unknown:generic_marker"
    assert event["client_evidence_class"] == "unknown"
    assert event["invocation_id"] == relay[0].headers["x-speko-invocation-id"]
    assert event["invocation_id"] != "00000000-0000-4000-8000-000000000000"
    assert invocation_headers() == {}


@pytest.mark.parametrize(
    "host, expected",
    [
        (b"MCP.Speko.AI", "mcp.speko.ai"),
        (b"localhost:8080", "localhost:8080"),
        (b"[::1]:8080", "[::1]:8080"),
        (b"[::1]invalid", None),
        (b"mcp.speko.ai.", "mcp.speko.ai."),
        (b"mcp.speko.ai/secret", None),
        (b"user:pass@mcp.speko.ai", None),
        (b"mcp.speko.ai:0", None),
        (b"mcp.speko.ai:99999", None),
        (b"mcp.speko.ai:", None),
        (b"mcp.speko.ai\r\nsecret", None),
        (b" mcp.speko.ai", None),
        (b"mcp..speko.ai", None),
        (b"-mcp.speko.ai", None),
        (b"mcp.speko.ai,evil.example", None),
        (b"a" * 201, None),
        (b"mcp.speko.\xff", None),
    ],
)
def test_observed_host_is_bounded_syntactic_evidence(host, expected) -> None:
    assert observed_host([(b"host", host)]) == expected


def test_duplicate_or_missing_host_is_unknown() -> None:
    assert observed_host([(b"host", b"mcp.speko.ai"), (b"Host", b"chatgpt.speko.ai")]) is None
    assert observed_host([(b"x-forwarded-host", b"mcp.speko.ai")]) is None


@pytest.mark.parametrize("failure", [None, RuntimeError("edge error"), asyncio.CancelledError()])
async def test_asgi_host_binding_resets_on_success_error_and_cancel(failure) -> None:
    async def app(scope, receive, send):
        assert invocation_headers()["X-Speko-MCP-Host"] == "chatgpt.speko.ai"
        if failure:
            raise failure

    guard = MCPProtocolGuard(app)
    scope = {
        "type": "http",
        "path": "/mcp",
        "method": "POST",
        "headers": [
            (b"host", b"chatgpt.speko.ai"),
            (b"x-speko-mcp-host", b"forged.example"),
        ],
    }
    if failure:
        with pytest.raises(type(failure)):
            await guard(scope, None, None)
    else:
        await guard(scope, None, None)
    assert invocation_headers() == {}
