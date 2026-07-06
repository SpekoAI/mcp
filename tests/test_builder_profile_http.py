"""End-to-end HTTP tests for the builder profile.

Unlike `test_builder_profile.py` (which monkeypatches the profile
resolution), these tests run the real ASGI app under a real uvicorn
server and speak actual streamable-http MCP, so the `?profile=builder`
query param is exercised through Starlette routing,
`RequestContextMiddleware`, and `get_http_request` — the same code path
production traffic takes. Auth uses a stub verifier (any bearer token)
so no network access is needed; the auth middleware itself still runs.
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

from spekoai_mcp.action_tools import ACTION_TOOL_NAMES
from spekoai_mcp.docs_tools import DOCS_TOOL_NAMES
from spekoai_mcp.profiles import BUILDER_PROFILE_TOOL_NAMES
from spekoai_mcp.server import create_app

HEADERS = {"Authorization": "Bearer sk_test_builder_profile"}


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
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="on"
    )
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


async def test_default_mcp_tool_list_is_unchanged_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]
    assert names == ACTION_TOOL_NAMES + DOCS_TOOL_NAMES


async def test_unknown_profile_value_is_default_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=ops", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]
    assert names == ACTION_TOOL_NAMES + DOCS_TOOL_NAMES


async def test_builder_profile_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=builder", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]
        assert names == BUILDER_PROFILE_TOOL_NAMES

        result = await client.call_tool("code_snippets.get", {"framework": "nextjs"})
        code = (result.structured_content or {})["code"]
        assert "VoiceConversation.create" in code
        assert "/v1/sessions" in code

        with pytest.raises(Exception, match="Unknown tool: 'agents.delete'"):
            await client.call_tool("agents.delete", {"agent_id": "x"})


async def test_builder_only_tool_hidden_on_default_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)
    ) as client:
        with pytest.raises(Exception, match="Unknown tool: 'code_snippets.get'"):
            await client.call_tool("code_snippets.get", {"framework": "curl"})
