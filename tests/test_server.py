"""Tests for the hosted Speko MCP server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, MultiAuth, RemoteAuthProvider, TokenVerifier
from starlette.testclient import TestClient

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.gateway_tools import GATEWAY_TOOL_NAMES
from spekoai_mcp.server import MCP_PATH, MCP_PROTOCOL_VERSION, create_app, create_server


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


def remote_oauth_auth() -> MultiAuth:
    return MultiAuth(
        server=RemoteAuthProvider(
            token_verifier=StubVerifier(),
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


async def test_server_lists_operational_and_docs_tools() -> None:
    mcp = create_server()
    names = [tool.name for tool in await mcp.list_tools()]
    assert names == ACTION_TOOL_NAMES + DOCS_TOOL_NAMES + GATEWAY_TOOL_NAMES
    assert all(not name.startswith("speko_") for name in names)
    assert "docs.search" in names
    assert "search_docs" not in names
    assert "create_agent" not in names
    assert "private_mcp_setup" not in names
    assert "recommended_stack" not in names
    assert "scaffold_voice_app" not in names


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
                "balanceUsd": 5,
                "currency": "USD",
                "updatedAt": "2026-05-14T16:00:00.000Z",
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

    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.speko.dev/v1/credits/balance"
    assert captured["headers"] == {"Authorization": "Bearer sk_platform_test"}
    assert payload == {
        "balanceUsd": 5,
        "currency": "USD",
        "updatedAt": "2026-05-14T16:00:00.000Z",
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


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({}, -32020),
        ({"MCP-Protocol-Version": "2025-11-25"}, -32022),
        ({"MCP-Protocol-Version": "2099-01-01"}, -32022),
        (
            {
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Session-Id": "legacy-session",
            },
            -32020,
        ),
    ],
)
def test_protocol_guard_rejects_legacy_shapes(
    headers: dict[str, str],
    expected_code: int,
) -> None:
    request_headers = modern_headers("server/discover")
    request_headers.pop("MCP-Protocol-Version")
    request_headers.update(headers)
    with TestClient(create_app(auth=StubVerifier())) as client:
        response = client.post(
            MCP_PATH, headers=request_headers, json=modern_request("server/discover")
        )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == expected_code
    if expected_code == -32022:
        assert response.json()["error"]["data"]["supported"] == [MCP_PROTOCOL_VERSION]


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
