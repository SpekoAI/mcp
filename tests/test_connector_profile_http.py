"""End-to-end HTTP tests for the connector profile.

Unlike `test_builder_profile.py` (which monkeypatches the profile
resolution), these tests run the real ASGI app under a real uvicorn
server and speak actual streamable-http MCP, so the `?profile=connector`
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

from spekoai_mcp.action_tools import DISCLOSURE_OPENER, DISCLOSURE_RULE
from spekoai_mcp.profiles import CONNECTOR_EXCLUDED_PREFIXES
from spekoai_mcp.server import create_app

HEADERS = {"Authorization": "Bearer sk_test_connector_profile"}


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


async def test_connector_profile_hides_evals_and_monitors_over_http(http_base_url: str) -> None:
    """The published directory surface must expose no bulk or scheduled tooling."""
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=connector", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]

    assert names, "connector profile advertised no tools at all"
    hidden = [n for n in names if n.startswith(CONNECTOR_EXCLUDED_PREFIXES)]
    assert hidden == [], f"bulk/scheduled tools leaked into the listing: {hidden}"


async def test_connector_profile_keeps_outbound_calling_over_http(http_base_url: str) -> None:
    """Cutting bulk tooling must not cost us the outbound-calling wedge."""
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=connector", headers=HEADERS)
    ) as client:
        names = [tool.name for tool in await client.list_tools()]

    for kept in ("sessions.phone.create", "agents.create", "agents.test_call"):
        assert kept in names, f"{kept} missing from the connector surface"


async def test_excluded_tool_is_uncallable_on_connector_over_http(http_base_url: str) -> None:
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp?profile=connector", headers=HEADERS)
    ) as client:
        with pytest.raises(Exception, match="Unknown tool: 'agents.evals.run'"):
            await client.call_tool("agents.evals.run", {"agent_id": "x", "eval_id": "y"})


async def test_disclosure_resolves_from_the_query_string(http_base_url: str) -> None:
    """The gate must read the real query param, not a patched helper.

    `test_ai_disclosure.py` monkeypatches `current_profile`, so it never proves
    that `?profile=connector` survives Starlette routing and reaches
    `get_http_request()`. This does.
    """
    from spekoai_mcp import action_tools

    captured: list[dict] = []

    async def fake_call(method, path, *, body=None, text=None, **kwargs):  # noqa: ANN001
        captured.append({"path": path, "body": body})
        from fastmcp.tools.tool import ToolResult

        return ToolResult(structured_content={"stubbed": True})

    original = action_tools.call
    action_tools.call = fake_call  # type: ignore[assignment]
    try:
        async with Client(
            StreamableHttpTransport(f"{http_base_url}/mcp?profile=connector", headers=HEADERS)
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

        captured.clear()
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
        sent = captured[-1]["body"]
        assert sent["firstMessage"] == "Hi, this is Ava.", sent["firstMessage"]
        assert "systemPrompt" not in sent
    finally:
        action_tools.call = original  # type: ignore[assignment]
