"""Tests for the hosted Speko MCP server."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, MultiAuth, RemoteAuthProvider, TokenVerifier
from starlette.testclient import TestClient

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import BUILDER_PROFILE_TOOL_NAMES, DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import (
    MCP_PATH,
    MCP_PROTOCOL_VERSION,
    PUBLIC_MCP_PATH,
    MCPProtocolGuard,
    create_app,
    create_public_server,
    create_server,
)

LEGACY_MCP_PROTOCOL_VERSION = "2025-11-25"


class StubVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "sk_valid":
            return None
        return AccessToken(
            token=token,
            client_id="api-key:key_1",
            scopes=["gateway.keys.manage"],
            claims={"organization_id": "org_1"},
        )


class StubOAuthVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "oauth_valid":
            return None
        return AccessToken(
            token=token,
            client_id="cursor-test-client",
            scopes=["openid", "profile", "email"],
            claims={"sub": "user_1", "auth_method": "oauth"},
        )


def remote_oauth_auth() -> MultiAuth:
    return MultiAuth(
        server=RemoteAuthProvider(
            token_verifier=StubOAuthVerifier(),
            authorization_servers=["https://platform.example/api/auth"],
            base_url="https://mcp.example",
            scopes_supported=["openid", "profile", "email"],
            challenge_scopes=["openid", "profile", "email"],
        ),
        verifiers=[StubVerifier()],
        base_url="https://mcp.example",
    )


def modern_request(method: str, params: dict[str, object] | None = None) -> dict[str, object]:
    merged_params = dict(params or {})
    merged_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "pytest", "version": "1"},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": merged_params}


def modern_headers(method: str, *, token: str = "sk_valid") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def legacy_initialize_request() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY_MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cursor", "version": "3.17.8"},
        },
    }


def legacy_headers(*, token: str = "sk_valid") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": LEGACY_MCP_PROTOCOL_VERSION,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def legacy_initialize_headers(*, token: str = "sk_valid") -> dict[str, str]:
    headers = legacy_headers(token=token)
    headers.pop("MCP-Protocol-Version")
    return headers


async def test_server_lists_operational_and_docs_tools() -> None:
    mcp = create_server()
    names = [tool.name for tool in await mcp.list_tools()]
    assert names == ACTION_TOOL_NAMES + DOCS_TOOL_NAMES
    assert all(not name.startswith("gateway.keys.") for name in names)
    assert all(not name.startswith("speko_") for name in names)
    assert "docs.search" in names
    assert "search_docs" not in names
    assert "create_agent" not in names
    assert "private_mcp_setup" not in names
    assert "recommended_stack" not in names
    assert "scaffold_voice_app" not in names


async def test_public_server_is_docs_only() -> None:
    mcp = create_public_server()
    names = [tool.name for tool in await mcp.list_tools()]
    assert names == DOCS_TOOL_NAMES
    assert all(tool.annotations.read_only_hint is True for tool in await mcp.list_tools())
    resources = await mcp.list_resources()
    assert [str(resource.uri) for resource in resources] == ["spekoai://docs/index"]


async def test_tools_expose_quality_metadata() -> None:
    tools = await create_server().list_tools()

    assert all(tool.title for tool in tools)
    assert all(tool.output_schema for tool in tools)
    assert all(tool.output_schema["type"] == "object" for tool in tools)
    assert all(tool.annotations is not None for tool in tools)

    by_name = {tool.name: tool for tool in tools}
    assert by_name["organization.get"].annotations.read_only_hint is True
    assert by_name["organization.get"].annotations.destructive_hint is False
    assert by_name["agents.create"].annotations.read_only_hint is False
    assert by_name["agents.create"].annotations.destructive_hint is False
    assert by_name["agents.delete"].annotations.destructive_hint is True
    assert by_name["docs.search"].annotations.open_world_hint is False
    assert by_name["docs.search"].output_schema["properties"]["result"]["type"] == "array"


async def test_docs_resources_are_advertised() -> None:
    mcp = create_server()
    resources = await mcp.list_resources()
    assert any(str(r.uri) == "spekoai://docs/index" for r in resources)
    templates = await mcp.list_resource_templates()
    assert any(t.uri_template == "spekoai://docs/{slug}" for t in templates)


async def test_get_credit_balance_forwards_auth_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        http_client,
        "get_access_token",
        lambda: SimpleNamespace(token="sk_platform_test"),
    )
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""
        reason_phrase = "OK"
        content = b"{}"

        def json(self) -> dict[str, object]:
            return {
                "kind": "result",
                "data": {
                    "balanceUsd": 5,
                    "currency": "USD",
                    "updatedAt": "2026-05-14T16:00:00.000Z",
                },
                "warnings": [],
                "resourceLinks": [],
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool, transport: object) -> None:
            captured["timeout"] = timeout
            captured["follow_redirects"] = follow_redirects
            captured["transport"] = transport

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
            json: object,
        ) -> FakeResponse:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(http_client.httpx, "AsyncClient", FakeAsyncClient)

    result = await create_server().call_tool("credits.balance.get", {})
    payload = result.structured_content or {}

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.speko.dev/v1/actions/credits.balance.get"
    assert captured["headers"] == {
        "Authorization": "Bearer sk_platform_test",
        "X-Speko-Source": "mcp",
        "X-Speko-MCP-Profile": "default",
        "X-Speko-Client": "unknown-mcp-client",
        "X-Speko-Action-Id": "credits.balance.get",
    }
    assert captured["json"] == {}
    assert payload == {
        "kind": "result",
        "data": {
            "balanceUsd": 5,
            "currency": "USD",
            "updatedAt": "2026-05-14T16:00:00.000Z",
        },
        "warnings": [],
        "resourceLinks": [],
    }


async def test_api_errors_become_tool_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        http_client,
        "get_access_token",
        lambda: SimpleNamespace(token="sk_platform_test"),
    )

    class FakeAsyncClient:
        def __init__(self, *, timeout: float, follow_redirects: bool, transport: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def request(self, *_args: object, **_kwargs: object) -> object:
            raise http_client.httpx.ConnectError("tls failed")

    monkeypatch.setattr(http_client.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(ToolError, match="Speko API returned 0"):
        await create_server().call_tool("credits.balance.get", {})


def test_asgi_health_is_public() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "OK"


def test_asgi_mcp_rejects_missing_bearer() -> None:
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers={
                key: value
                for key, value in modern_headers("server/discover").items()
                if key != "Authorization"
            },
            json=modern_request("server/discover"),
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == -32001


def test_public_mcp_lists_and_reads_docs_without_authentication() -> None:
    headers = legacy_headers()
    headers.pop("Authorization")
    with TestClient(create_app(auth=StubVerifier())) as client:
        initialize = client.post(
            PUBLIC_MCP_PATH,
            headers=headers,
            json=legacy_initialize_request(),
        )
        resources = client.post(
            PUBLIC_MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}},
        )
        read = client.post(
            PUBLIC_MCP_PATH,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": "spekoai://docs/index"},
            },
        )

    assert initialize.status_code == 200
    assert "resources" in initialize.json()["result"]["capabilities"]
    assert resources.status_code == 200
    listed = resources.json()["result"]["resources"]
    assert len(listed) == 1
    assert listed[0]["name"] == "docs_index"
    assert listed[0]["title"] == "SpekoAI documentation index"
    assert listed[0]["uri"] == "spekoai://docs/index"
    assert listed[0]["mimeType"] == "text/markdown"
    assert listed[0]["description"]
    assert read.status_code == 200
    content = read.json()["result"]["contents"][0]
    assert content["uri"] == "spekoai://docs/index"
    assert content["mimeType"] == "text/markdown"
    assert content["text"].startswith("# SpekoAI documentation index")

    # The same no-auth request must not gain access to operational tools.
    with TestClient(create_app(auth=StubVerifier())) as client:
        protected = client.post(
            MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )
    assert protected.status_code == 401


def test_public_mcp_root_post_alias_lists_docs_without_authentication() -> None:
    headers = legacy_headers()
    headers.pop("Authorization")
    with TestClient(create_app(auth=StubVerifier())) as client:
        initialize = client.post(
            "/",
            headers=headers,
            json=legacy_initialize_request(),
        )
        tools = client.post(
            "/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        resources = client.post(
            "/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        )

    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "spekoai-docs"
    assert tools.status_code == 200
    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == DOCS_TOOL_NAMES
    assert resources.status_code == 200
    assert resources.json()["result"]["resources"][0]["uri"] == "spekoai://docs/index"

    # GET / remains the human-facing documentation redirect.
    with TestClient(create_app(auth=StubVerifier()), follow_redirects=False) as client:
        redirect = client.get("/")
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "https://docs.speko.dev/quickstart/mcp"


def test_asgi_mcp_auth_path_is_not_mounted() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/mcp-auth")
    assert response.status_code == 404


def test_asgi_mcp_rejects_invalid_api_key() -> None:
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers=modern_headers("server/discover", token="sk_invalid"),
            json=modern_request("server/discover"),
        )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_asgi_serves_glama_manifest() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/.well-known/glama.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["$schema"] == "https://glama.ai/mcp/schemas/connector.json"
    assert body["maintainers"][0]["email"] == "abat@speko.ai"


def test_asgi_publishes_oauth_resource_metadata_but_keeps_as_external() -> None:
    with TestClient(create_app(auth=remote_oauth_auth())) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json() == {
            "resource": "https://mcp.example/mcp",
            "authorization_servers": ["https://platform.example/api/auth"],
            "scopes_supported": ["openid", "profile", "email"],
            "bearer_methods_supported": ["header"],
        }
        for path in ["/.well-known/oauth-authorization-server", "/authorize", "/token"]:
            assert client.get(path).status_code == 404


def test_oauth_challenge_points_clients_to_protected_resource_metadata() -> None:
    with TestClient(create_app(auth=remote_oauth_auth())) as client:
        response = client.post(
            MCP_PATH,
            headers={
                key: value
                for key, value in modern_headers("server/discover").items()
                if key != "Authorization"
            },
            json=modern_request("server/discover"),
        )
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert (
        'resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert 'scope="openid profile email"' in challenge


def test_legacy_oauth_initialize_gets_json_challenge() -> None:
    headers = legacy_initialize_headers()
    headers.pop("Authorization")
    with TestClient(create_app(auth=remote_oauth_auth())) as client:
        response = client.post(
            MCP_PATH,
            headers=headers,
            json=legacy_initialize_request(),
        )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == -32001
    challenge = response.headers["www-authenticate"]
    assert (
        'resource_metadata="https://mcp.example/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )
    assert 'scope="openid profile email"' in challenge


def test_modern_discover_is_json_only_and_sessionless() -> None:
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers=modern_headers("server/discover"),
            json=modern_request("server/discover"),
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    body = response.json()
    assert body["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]


def test_modern_tools_list_needs_no_initialize() -> None:
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers=modern_headers("tools/list"),
            json=modern_request("tools/list"),
        )
    assert response.status_code == 200
    assert any(tool["name"] == "organization.get" for tool in response.json()["result"]["tools"])
    assert "mcp-session-id" not in response.headers


def test_modern_tool_call_needs_no_initialize() -> None:
    with TestClient(create_app(auth=StubVerifier())) as client:
        headers = modern_headers("tools/call")
        headers["Mcp-Name"] = "docs.search"
        response = client.post(
            MCP_PATH,
            headers=headers,
            json=modern_request(
                "tools/call",
                {"name": "docs.search", "arguments": {"query": "voice agent"}},
            ),
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "result" in response.json()
    assert "mcp-session-id" not in response.headers


@pytest.mark.parametrize(
    ("auth", "token"),
    [
        (StubVerifier(), "sk_valid"),
        (remote_oauth_auth(), "oauth_valid"),
    ],
)
def test_legacy_cursor_sequence_is_authenticated_and_sessionless(
    auth: TokenVerifier | MultiAuth,
    token: str,
) -> None:
    initialize_headers = legacy_initialize_headers(token=token)
    initialize_headers["User-Agent"] = "Cursor/3.17.8"
    with TestClient(create_app(auth=auth)) as client:
        initialize = client.post(
            MCP_PATH,
            headers=initialize_headers,
            json=legacy_initialize_request(),
        )
        initialized = client.post(
            MCP_PATH,
            headers=legacy_headers(token=token),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        tools_list = client.post(
            MCP_PATH,
            headers=legacy_headers(token=token),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tool_call = client.post(
            MCP_PATH,
            headers=legacy_headers(token=token),
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "docs.search", "arguments": {"query": "voice agent"}},
            },
        )

    assert initialize.status_code == 200
    assert initialize.json()["result"]["protocolVersion"] == LEGACY_MCP_PROTOCOL_VERSION
    assert initialized.status_code == 202
    assert tools_list.status_code == 200
    assert any(tool["name"] == "organization.get" for tool in tools_list.json()["result"]["tools"])
    assert tool_call.status_code == 200
    assert "result" in tool_call.json()
    assert all(
        "mcp-session-id" not in response.headers
        for response in (initialize, initialized, tools_list, tool_call)
    )


def test_legacy_success_emits_sanitized_compatibility_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    user_agent = "Cursor/3.17.8 " + "x" * 200
    headers = legacy_initialize_headers()
    headers["User-Agent"] = user_agent
    with caplog.at_level(logging.INFO, logger="spekoai_mcp.server"):
        with TestClient(create_app(auth=StubVerifier())) as client:
            response = client.post(
                MCP_PATH,
                headers=headers,
                json=legacy_initialize_request(),
            )

    assert response.status_code == 200
    events = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "mcp_legacy_protocol_request_accepted"
    ]
    assert len(events) == 1
    assert events[0].protocol_era == "legacy"
    assert events[0].user_agent == user_agent[:128]


def test_legacy_telemetry_replaces_user_agent_control_characters() -> None:
    scope = {"headers": [(b"user-agent", b"Cursor/3.17.8\x00private\nvalue")]}
    assert MCPProtocolGuard._sanitized_user_agent(scope) == "Cursor/3.17.8?private?value"


def test_invalid_deployment_profile_fails_during_app_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "costumer")

    with pytest.raises(RuntimeError, match=DEFAULT_PROFILE_ENV_VAR):
        create_app(auth=StubVerifier())


@pytest.mark.parametrize(
    ("deployment_profile", "expected_names"),
    [
        (None, None),
        ("builder", BUILDER_PROFILE_TOOL_NAMES),
    ],
)
def test_tool_profiles_match_in_both_protocol_eras(
    deployment_profile: str | None,
    expected_names: list[str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if deployment_profile is None:
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, deployment_profile)
    with TestClient(create_app(auth=StubVerifier())) as client:
        modern = client.post(
            MCP_PATH,
            headers=modern_headers("tools/list"),
            json=modern_request("tools/list"),
        )
        legacy = client.post(
            MCP_PATH,
            headers=legacy_headers(),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert modern.status_code == legacy.status_code == 200
    modern_names = [tool["name"] for tool in modern.json()["result"]["tools"]]
    legacy_names = [tool["name"] for tool in legacy.json()["result"]["tools"]]
    assert legacy_names == modern_names
    if expected_names is not None:
        assert legacy_names == expected_names
    assert "mcp-session-id" not in modern.headers
    assert "mcp-session-id" not in legacy.headers


def test_requests_are_independent_across_app_instances() -> None:
    responses = []
    for _ in range(2):
        with TestClient(create_app(auth=StubVerifier())) as client:
            responses.append(
                client.post(
                    MCP_PATH,
                    headers=modern_headers("server/discover"),
                    json=modern_request("server/discover"),
                )
            )
    assert [response.status_code for response in responses] == [200, 200]
    assert all("mcp-session-id" not in response.headers for response in responses)


def test_malformed_modern_envelope_is_rejected() -> None:
    request = modern_request("server/discover")
    request["params"] = {"_meta": {}}
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers=modern_headers("server/discover"),
            json=request,
        )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == -32602


def test_modern_header_envelope_version_mismatch_is_rejected() -> None:
    request = modern_request("server/discover")
    request["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = (
        LEGACY_MCP_PROTOCOL_VERSION
    )
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers=modern_headers("server/discover"),
            json=request,
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


def test_unsupported_modern_version_is_rejected() -> None:
    requested = "2099-01-01"
    request = modern_request("server/discover")
    request["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = requested
    headers = modern_headers("server/discover")
    headers["MCP-Protocol-Version"] = requested
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(MCP_PATH, headers=headers, json=request)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32022
    assert response.json()["error"]["data"] == {
        "supported": [MCP_PROTOCOL_VERSION],
        "requested": requested,
    }


def test_protocol_guard_rejects_session_id() -> None:
    headers = legacy_headers()
    headers["Mcp-Session-Id"] = "legacy-session"
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


def test_protocol_guard_rejects_duplicate_version_header() -> None:
    headers = list(modern_headers("server/discover").items())
    headers.append(("MCP-Protocol-Version", MCP_PROTOCOL_VERSION))
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(MCP_PATH, headers=headers, json=modern_request("server/discover"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_mcp_allows_post_only(method: str) -> None:
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.request(method, MCP_PATH, headers=modern_headers("server/discover"))
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    assert response.headers["content-type"].startswith("application/json")
