"""Tests for `spekoai_mcp.server`.

The hosted app exposes public knowledge surfaces at `/mcp` and, when
OAuth is configured, the same public surface plus identity-aware action
tools at `/mcp-auth`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.auth import build_auth
from spekoai_mcp.server import AUTH_MCP_PATH, create_app, create_server


def _set_valid_oauth_env(monkeypatch) -> None:
    monkeypatch.setenv("SPEKOAI_OAUTH_ISSUER", "https://example.com/api/auth/oauth2")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("SPEKOAI_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://mcp.example.com")


async def test_public_server_excludes_private_tools() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "private_mcp_setup",
        "search_docs",
        "list_packages",
        "recommended_stack",
        "scaffold_voice_app",
    }
    assert not (names & set(ACTION_TOOL_NAMES))


async def test_private_server_includes_get_balance() -> None:
    mcp = create_server(include_private_tools=True)
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    for name in ACTION_TOOL_NAMES:
        assert name in names
    assert {
        "private_mcp_setup",
        "search_docs",
        "list_packages",
        "recommended_stack",
        "scaffold_voice_app",
    }.issubset(names)


async def test_get_balance_maps_api_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        http_client,
        "get_access_token",
        lambda: SimpleNamespace(token="upstream-oauth-token"),
    )
    monkeypatch.delenv("SPEKOAI_API_URL", raising=False)
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

    mcp = create_server(include_private_tools=True)
    result = await mcp.call_tool("get_balance", {})
    payload = result.structured_content or {}

    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.speko.dev/v1/credits/balance"
    assert captured["headers"] == {"Authorization": "Bearer upstream-oauth-token"}
    assert payload == {
        "balance_usd": 5.0,
        "currency": "USD",
        "updated_at": "2026-05-14T16:00:00.000Z",
    }


async def test_get_balance_reports_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    mcp = create_server(include_private_tools=True)
    with pytest.raises(ToolError, match="Speko API returned 0"):
        await mcp.call_tool("get_balance", {})


async def test_resources_and_prompts_advertised() -> None:
    """The knowledge-layer surfaces (static docs + scaffolding prompt +
    component snippets) must show up on a bare `create_server()` — they
    don't depend on auth or runtime config."""
    mcp = create_server()
    resources = await mcp.list_resources()
    resource_uris = {str(r.uri) for r in resources}
    assert "spekoai://docs/index" in resource_uris
    assert "spekoai://components/react/voice-session" in resource_uris
    templates = await mcp.list_resource_templates()
    assert any(t.uri_template == "spekoai://docs/{slug}" for t in templates)
    prompts = await mcp.list_prompts()
    assert any(p.name == "scaffold_project" for p in prompts)


async def test_list_packages_returns_structured_manifest() -> None:
    mcp = create_server()
    result = await mcp._call_tool_mcp("list_packages", {})  # type: ignore[attr-defined]
    # `structuredContent` from FastMCP wraps the return value under a
    # `result` key; the tool returns `list[PackageInfo]`.
    payload = result.structuredContent or {}
    rows = payload.get("result", [])
    assert rows, "list_packages returned no rows"
    names = {row["package_name"] for row in rows}
    assert "@spekoai/sdk" in names
    assert "@spekoai/client" in names
    assert "@spekoai/adapter-livekit" in names
    # Internal packages must NOT be exposed — `@spekoai/core` and
    # `@spekoai/providers` are `"private": true` and their docs are
    # deliberately not bundled into the public MCP.
    assert "@spekoai/core" not in names
    assert "@spekoai/providers" not in names


async def test_private_mcp_setup_advertises_get_balance(monkeypatch) -> None:
    monkeypatch.delenv("SPEKOAI_MCP_BASE_URL", raising=False)
    mcp = create_server()
    result = await mcp.call_tool("private_mcp_setup", {})
    payload = result.structured_content or {}
    assert payload["authenticated_endpoint"] == "https://mcp.speko.ai/mcp-auth"
    assert payload["recommended_action"] == "replace_public_mcp_with_authenticated_mcp"
    assert "self_hosted_path" not in payload
    tool_names = {tool["name"] for tool in payload["private_tools"]}
    for name in ACTION_TOOL_NAMES:
        assert name in tool_names
    assert "replace/switch your current public SpekoAI MCP connection" in payload[
        "user_prompt"
    ]
    assert "OAuth or a Speko API key" in payload["user_prompt"]
    assert "get_balance" in payload["user_prompt"]
    assert "speko_migrate" in tool_names
    assert any("superset of /mcp" in note for note in payload["notes"])
    assert any("Authorization: Bearer <key>" in note for note in payload["notes"])


async def test_private_mcp_setup_uses_configured_base_url(monkeypatch) -> None:
    monkeypatch.setenv("SPEKOAI_MCP_BASE_URL", "https://custom.example/")
    mcp = create_server()
    result = await mcp.call_tool("private_mcp_setup", {})
    payload = result.structured_content or {}
    assert payload["authenticated_endpoint"] == "https://custom.example/mcp-auth"


def test_asgi_health_is_public() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "OK"


def test_asgi_public_mcp_is_reachable_without_auth() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/mcp")
    assert response.status_code != 401
    assert response.status_code != 404
    assert "www-authenticate" not in response.headers


def test_asgi_auth_mcp_absent_without_oauth() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get(AUTH_MCP_PATH)
    assert response.status_code == 404


def test_asgi_auth_mcp_rejects_missing_bearer(monkeypatch) -> None:
    _set_valid_oauth_env(monkeypatch)
    auth = build_auth(mcp_path=AUTH_MCP_PATH)
    assert auth is not None

    app = create_app(auth=auth)
    with TestClient(app) as client:
        response = client.get(AUTH_MCP_PATH)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer ")


def test_asgi_oauth_metadata_advertises_auth_resource(monkeypatch) -> None:
    _set_valid_oauth_env(monkeypatch)
    auth = build_auth(mcp_path=AUTH_MCP_PATH)
    assert auth is not None

    app = create_app(auth=auth)
    with TestClient(app) as client:
        response = client.get("/.well-known/oauth-protected-resource/mcp-auth")
    assert response.status_code == 200
    assert "https://mcp.example.com/mcp-auth" in response.text
