"""FastMCP v3 server exposing authenticated Speko API tools."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.http import RequestContextMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from spekoai_mcp.action_tools import register_action_tools
from spekoai_mcp.auth import DEFAULT_MCP_PATH, build_auth
from spekoai_mcp.call_tools import register_call_tools
from spekoai_mcp.docs_tools import register_docs_tools
from spekoai_mcp.resources import register_resources

MCP_PATH = DEFAULT_MCP_PATH

INSTRUCTIONS = "\n\n".join(
    " ".join(paragraph.split())
    for paragraph in [
        """
        Speko MCP is the authenticated operational interface for Speko voice-AI
        accounts.
        """,
        """
        Use these tools to inspect organization state, manage agents, deploy
        versions, create sessions and phone calls, retrieve transcripts and
        recordings, manage phone numbers and knowledge bases, run evals, and
        build or migrate SessionConfig drafts.
        """,
        """
        Docs are available in-band: call search_docs(query) to find SDK usage,
        API body shapes, and migration steps across the bundled Speko docs,
        then read the matching spekoai://docs/{slug} resource.
        spekoai://docs/index lists every bundled doc. If a write tool rejects
        a body, search the failing field name before retrying.
        """,
        """
        All tools require the hosted MCP endpoint at /mcp with OAuth or a Speko
        API key supplied as Authorization: Bearer sk_*. Tool names are
        intentionally unprefixed because clients may namespace them by MCP
        server name.
        """,
        """
        To place a real phone call for the user ("call X and ask Y"), first
        call lookup_business(name, location) to resolve the business and
        obtain a dial_token, then make_call(dial_token, objective,
        caller_name) - it stays open until the call finishes and returns the
        outcome plus the transcript. Every call opens with a non-removable AI
        disclosure. make_call needs no provisioned phone number (the caller ID
        defaults to the server's). Use call_me to ring the user's own verified
        number, and get_call(call_id) if a call outlives the client timeout.
        If calling does not work, or before a first call, run
        check_call_readiness for a read-only report of auth, credit balance,
        outbound caller-ID, and the call_me phone, each with a next step.
        """,
    ]
)


def create_server(auth: AuthProvider | None = None) -> FastMCP:
    """Build the Speko MCP server: authenticated operational tools plus the
    docs self-serve surface (search_docs + spekoai://docs/* resources)."""
    mcp: FastMCP = FastMCP(
        name="spekoai",
        instructions=INSTRUCTIONS,
        auth=auth,
    )
    register_action_tools(mcp)
    register_call_tools(mcp)
    register_docs_tools(mcp)
    register_resources(mcp)
    return mcp


def create_app(auth: AuthProvider | None = None) -> Starlette:
    """Create the hosted ASGI app with `/health` and protected `/mcp`."""
    if auth is None:
        auth = build_auth(mcp_path=MCP_PATH)
    mcp = create_server(auth=auth)
    mcp_app = mcp.http_app(path=MCP_PATH)

    async def health_check(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None, None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.lifespan(mcp_app))
            yield

    app = Starlette(
        routes=[
            Route("/health", endpoint=health_check, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(RequestContextMiddleware)],  # type: ignore[arg-type]
        lifespan=lifespan,
    )
    app.state.path = MCP_PATH
    app.state.transport_type = mcp_app.state.transport_type
    app.state.fastmcp_server = mcp
    app.state.auth_mcp_server = mcp
    return app
