"""End-to-end HTTP tests for the Anthropic host's connector profile.

Unlike `test_builder_profile.py` (which monkeypatches the profile
resolution), these tests run the real ASGI app under a real uvicorn
server and speak actual streamable-http MCP. The deployment environment selects
the surface while the URL stays at bare `/mcp`, matching production. Auth uses
a stub verifier (any bearer token) so no network access is needed; the auth
middleware itself still runs.
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
from spekoai_mcp.profiles import (
    CONNECTOR_EXCLUDED_PREFIXES,
    DEFAULT_PROFILE_ENV_VAR,
    DIRECTORY_WITHHELD_TOOL_NAMES,
)
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


async def test_connector_profile_hides_evals_and_monitors_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The published directory surface must expose no bulk or scheduled tooling."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        names = [tool.name for tool in await client.list_tools()]

    assert names, "connector profile advertised no tools at all"
    hidden = [n for n in names if n.startswith(CONNECTOR_EXCLUDED_PREFIXES)]
    assert hidden == [], f"bulk/scheduled tools leaked into the listing: {hidden}"


async def test_connector_profile_withholds_every_tool_the_directory_named(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tools Anthropic's MCP Directory enumerated on 2026-08-27.

    Minus `sessions.phone.create`, reinstated 2026-09-04 with server-injected
    disclosure — see `test_connector_profile_serves_outbound_calling_over_http`.
    Nothing else from the enumeration may appear.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        names = {tool.name for tool in await client.list_tools()}

    leaked = sorted(DIRECTORY_WITHHELD_TOOL_NAMES & names)
    assert leaked == [], f"tools the directory requires absent are advertised: {leaked}"


async def test_connector_profile_keeps_transcription_and_reads_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut must not overshoot what the directory explicitly blessed.

    "audio.transcribe (speech-to-text) is fine and should stay, as are the read
    tools — listing agents, sessions, transcripts, recordings, phone numbers,
    credits and usage."
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        names = {tool.name for tool in await client.list_tools()}

    for kept in (
        "audio.transcribe",
        "agents.list",
        "agents.get",
        "sessions.list",
        "sessions.transcript.get",
        "sessions.recording.get",
        "calls.get",
        "calls.recording.get",
        "phone_numbers.list",
        "credits.balance.get",
        "usage.summary.get",
    ):
        assert kept in names, f"{kept} was cut from the connector surface but should stay"


async def test_excluded_tool_is_uncallable_on_connector_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        with pytest.raises(Exception, match="Unknown tool: 'agents.evals.run'"):
            await client.call_tool("agents.evals.run", {"agent_id": "x", "eval_id": "y"})


async def test_directory_required_tool_is_uncallable_on_connector_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hidden must also mean uncallable, or the listing is cosmetic."""
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        with pytest.raises(Exception, match="Unknown tool: 'agents.deploy'"):
            await client.call_tool("agents.deploy", {"agent_id": "a"})


async def test_connector_profile_serves_outbound_calling_over_http(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Placing a call is the reason the connector is worth installing.

    A directory surface that can read the transcript of a call it cannot place
    is a viewer. `sessions.phone.create` is advertised and reachable; disclosure
    is injected on the path (see test_ai_disclosure) so the callee is told.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")
    async with Client(StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert "sessions.phone.create" in names, "the connector surface cannot place a call"
    # The rest of the live-call group stays out: a browser session token is
    # useless in a chat client, and test_call is two synthesized agents talking.
    assert "sessions.create" not in names
    assert "agents.test_call" not in names


async def test_connector_deployment_restricts_the_bare_base_endpoint(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the change: no query string anywhere in the contract.

    `SPEKOAI_MCP_DEFAULT_PROFILE=connector` is read per request, so the already
    running server picks it up — which is also what makes this a real test of
    the production code path rather than of app construction.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")

    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)
    ) as client:
        names = {tool.name for tool in await client.list_tools()}

    leaked = sorted(DIRECTORY_WITHHELD_TOOL_NAMES & names)
    assert leaked == [], f"restricted deployment served them at bare /mcp: {leaked}"
    assert "audio.transcribe" in names, "the restricted base endpoint lost transcription"

    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)
    ) as client:
        with pytest.raises(Exception, match="Unknown tool: 'audio.synthesize'"):
            await client.call_tool(
                "audio.synthesize", {"body": {"text": "hi", "intent": {"language": "en"}}}
            )


async def test_no_query_parameter_can_widen_or_retarget_a_restricted_deployment(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is deliberately no query-string profile selector.

    If a caller could opt out of the deployment default, the boundary would be
    exactly as unenforceable as the query param it replaced.
    """
    monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "connector")

    for attempt in ("customer", "builder", "chatgpt", "connector", "full", "default", ""):
        async with Client(
            StreamableHttpTransport(f"{http_base_url}/mcp?profile={attempt}", headers=HEADERS)
        ) as client:
            names = {tool.name for tool in await client.list_tools()}
        leaked = sorted(DIRECTORY_WITHHELD_TOOL_NAMES & names)
        assert leaked == [], f"?profile={attempt} widened a restricted deployment: {leaked}"


async def test_unset_env_var_leaves_the_base_endpoint_full(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No regression for Claude Code, Codex, Cursor, Composio, Docker, Paperclip."""
    monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
    async with Client(
        StreamableHttpTransport(f"{http_base_url}/mcp", headers=HEADERS)
    ) as client:
        names = {tool.name for tool in await client.list_tools()}

    for kept in ("audio.synthesize", "sessions.phone.create", "agents.create", "agents.deploy"):
        assert kept in names, f"{kept} vanished from the unrestricted base endpoint"


async def test_disclosure_resolves_from_the_host_profile(
    http_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate must read the deployment setting, not a patched helper.

    Retargeted from the connector host to the ChatGPT host when the connector
    cut removed `sessions.phone.create`: ChatGPT's directory allows
    outbound calling, so it is now the only directory profile that can exercise
    the disclosure path end to end.
    """
    from spekoai_mcp import action_tools

    captured: list[dict] = []

    async def fake_call(method, path, *, body=None, text=None, **kwargs):  # noqa: ANN001
        captured.append({"path": path, "body": body})
        from fastmcp.tools import ToolResult

        return ToolResult(structured_content={"stubbed": True})

    original = action_tools.call
    action_tools.call = fake_call  # type: ignore[assignment]
    try:
        monkeypatch.setenv(DEFAULT_PROFILE_ENV_VAR, "chatgpt")
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

        captured.clear()
        monkeypatch.delenv(DEFAULT_PROFILE_ENV_VAR, raising=False)
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
