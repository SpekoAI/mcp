"""End-to-end HTTP tests for the ChatGPT profile.

These tests run the real ASGI app under a real uvicorn server and speak actual
streamable-http MCP. The deployment environment selects the surface while the
URL stays at bare `/mcp`, matching the host OpenAI's crawler and ChatGPT use.
Auth uses a stub verifier so no network is needed; the auth middleware still
runs.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import AccessToken, MultiAuth, TokenVerifier

from spekoai_mcp.action_tools import DISCLOSURE_OPENER, DISCLOSURE_RULE
from spekoai_mcp.profiles import CHATGPT_PROFILE_TOOL_NAMES, DEFAULT_PROFILE_ENV_VAR
from spekoai_mcp.server import create_app

HEADERS = {"Authorization": "Bearer sk_test_chatgpt_profile"}


class _AnyTokenVerifier(TokenVerifier):
    """Accept any bearer token; the point is exercising the middleware
    chain, not credential checking (covered by test_auth.py)."""

    def __init__(self) -> None:
        super().__init__(required_scopes=["api_key"])

    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(
            token=token,
            client_id="test-client",
            scopes=["api_key"],
            expires_at=None,
            claims={},
        )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def http_base_url() -> Iterator[str]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(auth=MultiAuth(verifiers=[_AnyTokenVerifier()], base_url=base_url))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn test server failed to start within 15s")
        time.sleep(0.05)
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


async def test_chatgpt_profile_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        names = [tool.name for tool in await client.list_tools()]
    assert names == CHATGPT_PROFILE_TOOL_NAMES


async def test_query_parameter_cannot_retarget_chatgpt_host(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a known profile value cannot widen or switch a host's surface."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=customer", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]
    assert names == CHATGPT_PROFILE_TOOL_NAMES


async def test_excluded_tool_is_uncallable_on_chatgpt_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        with pytest.raises(Exception, match="Unknown tool: 'phone_numbers.create'"):
            await client.call_tool("phone_numbers.create", {"body": {}})


async def test_disclosure_resolves_from_the_chatgpt_deployment(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must read the real deployment setting, not a patched helper."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
    from spekoai_mcp import action_tools

    captured: list[dict] = []

    async def fake_call(method, path, *, body=None, text=None, **kwargs):  # noqa: ANN001
        captured.append({"path": path, "body": body})
        from fastmcp.tools import ToolResult

        return ToolResult(structured_content={"stubbed": True})

    original = action_tools.call
    action_tools.call = fake_call  # type: ignore[assignment]
    try:
        async with Client(
            StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)
        ) as client:
            await client.call_tool(
                "sessions.phone.create",
                {
                    "body": {
                        "to": "+12015551234",
                        "agentId": "a",
                        "firstMessage": "Hi, this is Ava.",
                    }
                },
            )
        assert captured, "sessions.phone.create never reached the relay"
        sent = captured[-1]["body"]
        assert sent["firstMessage"].startswith(DISCLOSURE_OPENER), sent["firstMessage"]
        assert DISCLOSURE_RULE in sent["systemPrompt"]
    finally:
        action_tools.call = original  # type: ignore[assignment]
