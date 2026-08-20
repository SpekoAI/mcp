"""Tests for scoped Runtime Gateway key tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.gateway_client as gateway_client
from spekoai_mcp.gateway_tools import GATEWAY_TOOL_NAMES
from spekoai_mcp.server import create_server


@pytest.fixture
def gateway_mock(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorization": request.headers.get("Authorization"),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.method == "POST":
            return httpx.Response(201, json={"id": "key_1", "key": "sk_speko_once"})
        return httpx.Response(204)

    monkeypatch.setattr(
        gateway_client,
        "get_access_token",
        lambda: SimpleNamespace(
            token="sk_platform_scoped",
            scopes=["gateway.keys.manage"],
        ),
    )
    gateway_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    yield calls
    gateway_client._TEST_TRANSPORT = None


async def test_gateway_key_tools_use_runtime_api(gateway_mock: list[dict[str, object]]) -> None:
    mcp = create_server()

    listed = await mcp.call_tool("gateway.keys.list", {})
    created = await mcp.call_tool("gateway.keys.create", {"name": " production "})
    revoked = await mcp.call_tool("gateway.keys.revoke", {"key_id": "key_1"})

    assert [call["method"] for call in gateway_mock] == ["GET", "POST", "DELETE"]
    assert [call["path"] for call in gateway_mock] == [
        "/api/keys",
        "/api/keys",
        "/api/keys/key_1",
    ]
    assert all(call["authorization"] == "Bearer sk_platform_scoped" for call in gateway_mock)
    assert gateway_mock[1]["body"] == {"name": "production"}
    assert listed.structured_content == {"data": []}
    assert created.structured_content == {"id": "key_1", "key": "sk_speko_once"}
    assert revoked.structured_content == {"id": "key_1", "revoked": True}


async def test_gateway_url_override(
    gateway_mock: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEKOAI_GATEWAY_URL", "https://gateway.staging.example/")
    await create_server().call_tool("gateway.keys.list", {})
    assert gateway_mock[-1]["path"] == "/api/keys"


@pytest.mark.parametrize("scopes", [[], ["api_key"]])
async def test_gateway_tools_require_manage_scope(
    monkeypatch: pytest.MonkeyPatch,
    scopes: list[str],
) -> None:
    monkeypatch.setattr(
        gateway_client,
        "get_access_token",
        lambda: SimpleNamespace(token="sk_platform_unscoped", scopes=scopes),
    )
    gateway_client._TEST_TRANSPORT = httpx.MockTransport(
        lambda _request: pytest.fail("unscoped token must not reach Runtime")
    )
    try:
        with pytest.raises(ToolError, match="gateway.keys.manage"):
            await create_server().call_tool("gateway.keys.list", {})
    finally:
        gateway_client._TEST_TRANSPORT = None


async def test_gateway_tools_are_default_surface_with_v4_metadata() -> None:
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert set(GATEWAY_TOOL_NAMES) <= tools.keys()
    assert tools["gateway.keys.list"].annotations.read_only_hint is True
    assert tools["gateway.keys.create"].annotations.destructive_hint is False
    assert tools["gateway.keys.revoke"].annotations.destructive_hint is True
    assert "router.keys.list" not in tools
    assert "gateway.keys.update" not in tools
