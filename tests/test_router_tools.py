"""Tests for the Router key provisioning tools (`router.keys.*`).

Three things carry weight here:

1. A Speko API key must never provision a router key, and must never reach
   the control plane at all (`test_*_refuses_api_key_*`).
2. A PARTIAL policy must be filled to all seven keys before it is sent —
   the control plane's `parseKeyPolicy` rejects a partial object, so
   without this an LLM loops on `invalid_policy`.
3. Bad policies are rejected locally, before the round trip, with the fix
   in the error message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import NotFoundError, ToolError

import spekoai_mcp.profiles as profiles
import spekoai_mcp.router_client as router_client
from spekoai_mcp.profiles import BUILDER_PROFILE_TOOL_NAMES
from spekoai_mcp.router_tools import (
    ROUTER_TOOL_NAMES,
    default_router_policy,
    normalize_policy,
)
from spekoai_mcp.server import create_server

KEY_SUMMARY = {
    "id": "key_1",
    "name": "pipecat-prod",
    "prefix": "sk_live_a1b2...",
    "createdAt": "2026-07-28T00:00:00.000Z",
    "lastUsedAt": None,
    "revokedAt": None,
    "policy": default_router_policy(),
}


@pytest.fixture
def control_plane_mock(monkeypatch: pytest.MonkeyPatch):
    """Record every control-plane request; answer with realistic shapes."""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content.decode("utf-8") or "null"),
            }
        )
        if request.method == "GET":
            return httpx.Response(200, json={"data": [KEY_SUMMARY]})
        if request.method == "POST":
            return httpx.Response(201, json={**KEY_SUMMARY, "key": "sk_live_full_secret"})
        if request.method == "PATCH":
            return httpx.Response(200, json=KEY_SUMMARY)
        return httpx.Response(204)

    monkeypatch.setattr(
        router_client,
        "get_access_token",
        lambda: SimpleNamespace(token="oauth-token", scopes=["openid"]),
    )
    router_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        router_client._TEST_TRANSPORT = None


@pytest.fixture
def api_key_caller(monkeypatch: pytest.MonkeyPatch):
    """A caller authenticated with a Speko API key instead of OAuth."""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append({"path": request.url.path})
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        router_client,
        "get_access_token",
        lambda: SimpleNamespace(token="sk_live_machine", scopes=["api_key"]),
    )
    router_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield calls
    finally:
        router_client._TEST_TRANSPORT = None


def _force_http_profile(monkeypatch: pytest.MonkeyPatch, profile: str | None) -> None:
    query_params: dict[str, str] = {} if profile is None else {"profile": profile}
    monkeypatch.setattr(
        profiles, "get_http_request", lambda: SimpleNamespace(query_params=query_params)
    )


# --- relay ------------------------------------------------------------------


async def test_router_key_tools_hit_the_control_plane(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    mcp = create_server()
    await mcp.call_tool("router.keys.list", {})
    await mcp.call_tool("router.keys.create", {"name": "pipecat-prod"})
    await mcp.call_tool("router.keys.update", {"key_id": "key_1", "name": "renamed"})
    await mcp.call_tool("router.keys.revoke", {"key_id": "key_1"})

    assert [(call["method"], call["path"]) for call in control_plane_mock] == [
        ("GET", "/api/keys"),
        ("POST", "/api/keys"),
        ("PATCH", "/api/keys/key_1"),
        ("DELETE", "/api/keys/key_1"),
    ]
    assert {call["auth"] for call in control_plane_mock} == {"Bearer oauth-token"}
    assert control_plane_mock[0]["url"].startswith("https://control.speko.ai/")


async def test_control_base_is_overridable(
    control_plane_mock: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPEKOAI_ROUTER_CONTROL_URL", "https://control.staging.speko.ai/")
    await create_server().call_tool("router.keys.list", {})
    assert control_plane_mock[0]["url"] == "https://control.staging.speko.ai/api/keys"


async def test_create_returns_the_one_time_secret(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    result = await create_server().call_tool("router.keys.create", {"name": "pipecat-prod"})
    assert (result.structured_content or {})["key"] == "sk_live_full_secret"


async def test_create_without_policy_sends_only_the_name(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    await create_server().call_tool("router.keys.create", {"name": "  pipecat-prod  "})
    assert control_plane_mock[0]["body"] == {"name": "pipecat-prod"}


async def test_revoke_reports_the_revoked_id_from_a_204(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    result = await create_server().call_tool("router.keys.revoke", {"key_id": "key_1"})
    assert result.structured_content == {"id": "key_1", "revoked": True}


async def test_key_ids_are_path_escaped(control_plane_mock: list[dict[str, Any]]) -> None:
    await create_server().call_tool("router.keys.revoke", {"key_id": "admin/overview"})
    assert control_plane_mock[0]["url"] == "https://control.speko.ai/api/keys/admin%2Foverview"


# --- partial policy is filled, not rejected ---------------------------------


async def test_partial_policy_is_filled_with_defaults_before_sending(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    """`parseKeyPolicy` requires all seven keys; a partial object 400s."""
    await create_server().call_tool(
        "router.keys.create",
        {"name": "es-latency", "policy": {"language": "es-MX", "objective": "latency"}},
    )

    assert control_plane_mock[0]["body"]["policy"] == {
        "language": "es-MX",
        "useCase": None,
        "objective": "latency",
        "maxPricePerMinUsd": None,
        "stt": {"chain": []},
        "llm": {"chain": []},
        "tts": {"chain": []},
    }


async def test_empty_policy_object_becomes_the_default_policy(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    await create_server().call_tool("router.keys.create", {"name": "default", "policy": {}})
    assert control_plane_mock[0]["body"]["policy"] == default_router_policy()


async def test_chain_accepts_the_contract_shape_and_a_bare_list(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    await create_server().call_tool(
        "router.keys.create",
        {
            "name": "pinned",
            "policy": {
                "stt": {"chain": ["deepgram:nova-3", "openai:gpt-4o-transcribe"]},
                "tts": ["cartesia:sonic-3.5"],
            },
        },
    )

    policy = control_plane_mock[0]["body"]["policy"]
    assert policy["stt"] == {"chain": ["deepgram:nova-3", "openai:gpt-4o-transcribe"]}
    assert policy["tts"] == {"chain": ["cartesia:sonic-3.5"]}
    assert policy["llm"] == {"chain": []}


async def test_update_sends_only_the_supplied_fields(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    await create_server().call_tool(
        "router.keys.update", {"key_id": "key_1", "policy": {"objective": "cost"}}
    )
    body = control_plane_mock[0]["body"]
    assert set(body) == {"policy"}
    assert body["policy"]["objective"] == "cost"


def test_normalize_policy_preserves_use_case_and_max_price() -> None:
    policy = normalize_policy({"useCase": "phone_agent", "maxPricePerMinUsd": 0.12})
    assert policy["useCase"] == "phone_agent"
    assert policy["maxPricePerMinUsd"] == 0.12


# --- local validation, before the round trip --------------------------------


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"objective": "cheap"}, "objective must be one of"),
        ({"useCase": "support"}, "useCase must be null or one of"),
        ({"language": "en_US_with_a_very_long_tag_value"}, "language must be a BCP-47 tag"),
        ({"maxPricePerMinUsd": -1}, "maxPricePerMinUsd must be null or a number"),
        ({"maxPricePerMinUsd": "0.12"}, "maxPricePerMinUsd must be null or a number"),
        ({"optimizeFor": "latency"}, "unknown field"),
        ({"stt": {"chain": ["nova-3"]}}, "candidate ids"),
        ({"stt": {"models": ["deepgram:nova-3"]}}, "accepts only a chain field"),
        ({"stt": {"chain": ["a:1", "b:2", "c:3", "d:4", "e:5"]}}, "at most 4 candidate ids"),
        ({"llm": {"chain": ["openai:gpt-4.1", "openai:gpt-4.1"]}}, "repeats"),
        ({"tts": "cartesia:sonic-3.5"}, "must be an array"),
    ],
)
async def test_invalid_policy_is_rejected_before_the_control_plane(
    control_plane_mock: list[dict[str, Any]],
    policy: dict[str, Any],
    expected: str,
) -> None:
    with pytest.raises(ToolError, match=expected):
        await create_server().call_tool(
            "router.keys.create", {"name": "probe", "policy": policy}
        )

    assert control_plane_mock == []


async def test_empty_update_is_rejected_before_the_control_plane(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    with pytest.raises(ToolError, match="pass name, policy, or both"):
        await create_server().call_tool("router.keys.update", {"key_id": "key_1"})

    assert control_plane_mock == []


async def test_blank_name_is_rejected_before_the_control_plane(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    with pytest.raises(ToolError, match="Invalid router key name"):
        await create_server().call_tool("router.keys.create", {"name": "   "})

    assert control_plane_mock == []


async def test_overlong_name_is_rejected_before_the_control_plane(
    control_plane_mock: list[dict[str, Any]],
) -> None:
    with pytest.raises(ToolError, match="at most 64 characters"):
        await create_server().call_tool("router.keys.create", {"name": "x" * 65})

    assert control_plane_mock == []


# --- an API key cannot mint router keys -------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("router.keys.list", {}),
        ("router.keys.create", {"name": "from-a-machine"}),
        ("router.keys.update", {"key_id": "key_1", "name": "renamed"}),
        ("router.keys.revoke", {"key_id": "key_1"}),
    ],
)
async def test_api_key_caller_is_refused_without_reaching_the_control_plane(
    api_key_caller: list[dict[str, Any]],
    tool: str,
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(ToolError, match="a Speko API key cannot mint router keys"):
        await create_server().call_tool(tool, arguments)

    assert api_key_caller == []


async def test_api_key_scope_is_refused_even_without_the_sk_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: the scope alone marks a machine principal."""
    monkeypatch.setattr(
        router_client,
        "get_access_token",
        lambda: SimpleNamespace(token="opaque-machine-token", scopes=["api_key"]),
    )
    with pytest.raises(ToolError, match="a Speko API key cannot mint router keys"):
        await create_server().call_tool("router.keys.list", {})


async def test_unauthenticated_caller_is_told_to_use_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_client, "get_access_token", lambda: None)
    with pytest.raises(ToolError, match="Connect /mcp with OAuth"):
        await create_server().call_tool("router.keys.list", {})


# --- control-plane errors carry the fix -------------------------------------


@pytest.fixture
def control_plane_error(monkeypatch: pytest.MonkeyPatch):
    def install(status_code: int, payload: dict[str, Any]) -> None:
        monkeypatch.setattr(
            router_client,
            "get_access_token",
            lambda: SimpleNamespace(token="oauth-token", scopes=["openid"]),
        )
        router_client._TEST_TRANSPORT = httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json=payload)
        )

    try:
        yield install
    finally:
        router_client._TEST_TRANSPORT = None


@pytest.mark.parametrize(
    ("status_code", "payload", "expected"),
    [
        (409, {"error": "key_limit", "limit": 10}, "Ten active router keys"),
        (400, {"error": "unroutable_model"}, "routable field is true"),
        (400, {"error": "invalid_policy"}, "omitted fields take their defaults"),
        (503, {"error": "catalog_unavailable"}, "catalog is momentarily unavailable"),
        (404, {"error": "not_found"}, "Call router.keys.list for current ids"),
        (429, {"error": "rate_limited"}, "Rate limited"),
    ],
)
async def test_control_plane_errors_carry_a_next_step(
    control_plane_error,
    status_code: int,
    payload: dict[str, Any],
    expected: str,
) -> None:
    control_plane_error(status_code, payload)
    with pytest.raises(ToolError, match=expected):
        await create_server().call_tool("router.keys.create", {"name": "probe"})


# --- surface ----------------------------------------------------------------


async def test_router_tools_are_on_the_default_surface_with_metadata() -> None:
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name in ROUTER_TOOL_NAMES:
        assert name in tools
        assert tools[name].title
        assert tools[name].output_schema["type"] == "object"

    assert tools["router.keys.list"].annotations.readOnlyHint is True
    assert tools["router.keys.create"].annotations.readOnlyHint is False
    assert tools["router.keys.create"].annotations.destructiveHint is False
    assert tools["router.keys.update"].annotations.destructiveHint is False
    assert tools["router.keys.revoke"].annotations.destructiveHint is True


async def test_router_tools_are_hidden_from_the_builder_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_http_profile(monkeypatch, "builder")
    mcp = create_server()
    names = [tool.name for tool in await mcp.list_tools()]
    assert names == BUILDER_PROFILE_TOOL_NAMES
    for name in ROUTER_TOOL_NAMES:
        assert name not in names
        with pytest.raises(NotFoundError, match=f"Unknown tool: '{name}'"):
            await mcp.call_tool(name, {})
