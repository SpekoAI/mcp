"""Tests for the hosted Speko MCP server."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.auth import build_auth
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.server import MCP_PATH, create_app, create_server


def _set_valid_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://mcp.example.com")


async def test_server_lists_operational_and_docs_tools() -> None:
    mcp = create_server()
    names = [tool.name for tool in await mcp.list_tools()]
    assert names == ACTION_TOOL_NAMES + DOCS_TOOL_NAMES
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
    assert by_name["organization.get"].annotations.readOnlyHint is True
    assert by_name["organization.get"].annotations.destructiveHint is False
    assert by_name["agents.create"].annotations.readOnlyHint is False
    assert by_name["agents.create"].annotations.destructiveHint is False
    assert by_name["agents.delete"].annotations.destructiveHint is True
    assert by_name["docs.search"].annotations.openWorldHint is False
    assert by_name["docs.search"].output_schema["properties"]["result"]["type"] == "array"


async def test_docs_resources_advertised_but_prompts_stay_disabled() -> None:
    mcp = create_server()
    resources = await mcp.list_resources()
    assert any(str(r.uri) == "spekoai://docs/index" for r in resources)
    templates = await mcp.list_resource_templates()
    assert any(t.uri_template == "spekoai://docs/{slug}" for t in templates)
    assert await mcp.list_prompts() == []


async def test_get_credit_balance_forwards_auth_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        http_client,
        "get_access_token",
        lambda: SimpleNamespace(token="upstream-oauth-token"),
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
    assert captured["headers"] == {"Authorization": "Bearer upstream-oauth-token"}
    assert payload == {
        "balanceUsd": 5,
        "currency": "USD",
        "updatedAt": "2026-05-14T16:00:00.000Z",
    }


async def test_api_errors_become_tool_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        http_client,
        "get_access_token",
        lambda: SimpleNamespace(token="upstream-oauth-token"),
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


def test_asgi_mcp_rejects_missing_bearer_without_oauth() -> None:
    with TestClient(create_app()) as client:
        response = client.get(MCP_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")


def test_asgi_mcp_auth_path_is_not_mounted() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/mcp-auth")
    assert response.status_code == 404


def test_asgi_mcp_rejects_missing_bearer_with_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_oauth_env(monkeypatch)
    auth = build_auth(mcp_path=MCP_PATH)

    with TestClient(create_app(auth=auth)) as client:
        response = client.get(MCP_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")


def test_asgi_serves_glama_manifest() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/.well-known/glama.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["$schema"] == "https://glama.ai/mcp/schemas/connector.json"
    assert body["maintainers"][0]["email"] == "abat@speko.ai"


def test_asgi_oauth_metadata_advertises_mcp_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_oauth_env(monkeypatch)
    auth = build_auth(mcp_path=MCP_PATH)

    with TestClient(create_app(auth=auth)) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    assert "https://mcp.example.com/mcp" in response.text
