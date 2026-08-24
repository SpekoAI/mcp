"""End-to-end HTTP tests for the ChatGPT profile.

`test_chatgpt_profile.py` monkeypatches the profile resolution, so it never
proves that `?profile=chatgpt` survives Starlette routing and reaches
`get_http_request()`. These run the real ASGI app under a real uvicorn server
and speak actual streamable-http MCP — the same code path OpenAI's crawler and
ChatGPT itself will take. Auth uses a stub verifier so no network is needed;
the auth middleware still runs.
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
from starlette.middleware.base import BaseHTTPMiddleware

from spekoai_mcp.action_tools import DISCLOSURE_OPENER, DISCLOSURE_RULE
from spekoai_mcp.profiles import CHATGPT_PROFILE_TOOL_NAMES
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


async def test_chatgpt_profile_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=chatgpt", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]
    assert names == CHATGPT_PROFILE_TOOL_NAMES


async def test_the_client_repeats_the_profile_query_on_every_request(http_base_url: str) -> None:
    """The design depends on this, so measure it rather than assume it.

    Profile selection is resolved per request from the query string. That is
    only safe if a client repeats the whole URL on every request of a session
    — and on protocol 2026-07-28 this server is stateless, so there is no
    session id to pin to as a fallback. If a client ever stopped repeating it,
    the surface would silently widen back to the full default one instead of
    erroring, which on a published directory profile means serving the tools
    the listing states are absent.
    """
    seen: list[tuple[str, str]] = []

    class _Log(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # noqa: ANN001, ANN202
            if request.url.path.endswith("/mcp"):
                seen.append((request.method, request.url.query))
            return await call_next(request)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    app = create_app(auth=MultiAuth(verifiers=[_AnyTokenVerifier()], base_url=base_url))
    app.add_middleware(_Log)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn test server failed to start within 15s")
        time.sleep(0.05)
    try:
        async with Client(
            StreamableHttpTransport(f"{base_url}/mcp?profile=chatgpt", headers=HEADERS)
        ) as client:
            names = [tool.name for tool in await client.list_tools()]
        assert names == CHATGPT_PROFILE_TOOL_NAMES
    finally:
        server.should_exit = True
        thread.join(timeout=5)

    assert seen, "no MCP requests were observed"
    without = [(m, q) for m, q in seen if "profile=chatgpt" not in q]
    assert without == [], f"the client dropped the profile on {without}"


async def test_excluded_tool_is_uncallable_on_chatgpt_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=chatgpt", headers=HEADERS)
    ) as client:
        with pytest.raises(Exception, match="Unknown tool: 'phone_numbers.create'"):
            await client.call_tool("phone_numbers.create", {"body": {}})


async def test_disclosure_resolves_from_the_chatgpt_query_string(http_base_url: str) -> None:
    """The gate must read the real query param, not a patched helper."""
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
            StreamableHttpTransport(f"{http_base_url}/mcp?profile=chatgpt", headers=HEADERS)
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
