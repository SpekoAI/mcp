"""FastMCP v4 server exposing OAuth/API-key authenticated stateless tools."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import lru_cache
from importlib.resources import files

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.http import RequestContextMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Message as ASGIMessage
from starlette.types import Receive, Scope, Send

from spekoai_mcp.action_tools import register_action_tools
from spekoai_mcp.auth import DEFAULT_MCP_PATH, build_auth
from spekoai_mcp.builder_tools import register_builder_tools
from spekoai_mcp.docs_tools import register_docs_tools
from spekoai_mcp.profiles import ToolProfileMiddleware
from spekoai_mcp.prompts import register_prompts
from spekoai_mcp.resources import register_resources

MCP_PATH = DEFAULT_MCP_PATH
PUBLIC_MCP_PATH = "/.well-known/mcp"
MCP_PROTOCOL_VERSION = "2026-07-28"
HEADER_MISMATCH = -32020
LEGACY_USER_AGENT_MAX_LENGTH = 128

logger = logging.getLogger(__name__)

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
        Docs are available in-band: call docs.search(query) to find SDK usage,
        API body shapes, and migration steps across the bundled Speko docs,
        then read the matching spekoai://docs/{slug} resource.
        spekoai://docs/index lists every bundled doc. If a write tool rejects
        a body, search the failing field name before retrying.
        """,
        """
        All tools require the hosted MCP endpoint at /mcp. Interactive clients
        may use OAuth; automation may supply a Speko API key as Authorization:
        Bearer sk_*. Tool names use
        domain.action dot notation, for example agents.list, sessions.create,
        docs.search, and knowledge_bases.documents.create.
        """,
    ]
)

PUBLIC_INSTRUCTIONS = "\n\n".join(
    " ".join(paragraph.split())
    for paragraph in [
        """
        Speko Docs MCP is the public, read-only discovery surface for Speko's
        voice-AI API, SDKs, adapters, quickstarts, and migration guides.
        """,
        """
        Read spekoai://docs/index first, then read the matching
        spekoai://docs/{slug} resource. Use docs.search when you know the job
        or field name but not the document slug.
        """,
        """
        This endpoint never exposes account data or write tools and requires no
        authentication. Use the authenticated operational MCP endpoint at
        https://mcp.speko.ai/mcp for agents, calls, sessions, phone numbers,
        knowledge bases, evals, and organization state.
        """,
    ]
)


@lru_cache(maxsize=1)
def _glama_manifest() -> str:
    """The Glama connector manifest, bundled in the wheel and served at
    `/.well-known/glama.json`. Hosting it here means glama.ai validates the
    connector against the hosted MCP origin (mcp.speko.dev) rather than the
    marketing site."""
    return (files("spekoai_mcp") / "_well_known" / "glama.json").read_text(encoding="utf-8")


def create_server(auth: AuthProvider | None = None) -> FastMCP:
    """Build the Speko MCP server: authenticated operational tools plus the
    docs self-serve surface (docs.search + spekoai://docs/* resources).

    One server serves two per-request tool profiles (see `profiles.py`):
    the default full surface, and a curated builder preset selected with
    `/mcp?profile=builder`. Builder-only tools are registered LAST so the
    default profile's tool ordering stays byte-identical after the
    middleware filters them out."""
    mcp: FastMCP = FastMCP(
        name="spekoai",
        instructions=INSTRUCTIONS,
        auth=auth,
    )
    register_action_tools(mcp)
    register_docs_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)
    register_builder_tools(mcp)
    mcp.add_middleware(ToolProfileMiddleware())
    return mcp


def create_public_server() -> FastMCP:
    """Build the unauthenticated, read-only documentation MCP surface."""
    mcp: FastMCP = FastMCP(
        name="spekoai-docs",
        instructions=PUBLIC_INSTRUCTIONS,
    )
    register_docs_tools(mcp)
    register_resources(mcp)
    return mcp


def create_app(auth: AuthProvider | None = None) -> Starlette:
    """Create the hosted ASGI app with public docs and protected operations."""
    if auth is None:
        auth = build_auth(mcp_path=MCP_PATH)
    mcp = create_server(auth=auth)
    mcp_app = mcp.http_app(
        path=MCP_PATH,
        stateless_http=True,
        json_response=True,
    )
    public_mcp = create_public_server()
    public_mcp_app = public_mcp.http_app(
        path=PUBLIC_MCP_PATH,
        stateless_http=True,
        json_response=True,
    )

    async def health_check(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    async def docs_redirect(_: Request) -> RedirectResponse:
        return RedirectResponse("https://docs.speko.dev/quickstart/mcp", status_code=307)

    async def glama_manifest(_: Request) -> Response:
        return Response(_glama_manifest(), media_type="application/json")

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None, None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp_app.lifespan(mcp_app))
            await stack.enter_async_context(public_mcp_app.lifespan(public_mcp_app))
            yield

    protocol_app = MCPPathRouter(
        protected=MCPProtocolGuard(mcp_app, path=MCP_PATH),
        public=MCPProtocolGuard(public_mcp_app, path=PUBLIC_MCP_PATH),
    )

    app = Starlette(
        routes=[
            Route("/", endpoint=docs_redirect, methods=["GET"]),
            Route("/health", endpoint=health_check, methods=["GET"]),
            Route("/.well-known/glama.json", endpoint=glama_manifest, methods=["GET"]),
            Mount("/", app=protocol_app),
        ],
        middleware=[Middleware(RequestContextMiddleware)],  # type: ignore[arg-type]
        lifespan=lifespan,
    )
    app.state.path = MCP_PATH
    app.state.transport_type = mcp_app.state.transport_type
    app.state.fastmcp_server = mcp
    app.state.auth_mcp_server = mcp
    app.state.public_mcp_server = public_mcp
    return app


ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class MCPProtocolGuard:
    """Keep the hosted endpoint POST-only and sessionless across both MCP eras."""

    def __init__(self, app: ASGIApp, *, path: str = MCP_PATH) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return

        if scope.get("method") != "POST":
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "POST required"},
                },
                status_code=405,
                headers={"Allow": "POST"},
            )
            await response(scope, receive, send)
            return

        headers = scope.get("headers", [])
        versions = [
            value.decode("latin-1")
            for name, value in headers
            if name.lower() == b"mcp-protocol-version"
        ]
        has_session = any(name.lower() == b"mcp-session-id" for name, _ in headers)
        if len(versions) > 1 or has_session:
            if has_session:
                message = "Mcp-Session-Id is not supported"
            else:
                message = "MCP-Protocol-Version header appears more than once"
            await self._jsonrpc_error(scope, receive, send, HEADER_MISMATCH, message)
            return
        await self._call_json_app(
            scope,
            receive,
            send,
            legacy_candidate=not versions,
        )

    async def _call_json_app(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        legacy_candidate: bool,
    ) -> None:
        """Keep transport-level authentication failures JSON-only too."""
        messages: list[ASGIMessage] = []

        async def capture(message: ASGIMessage) -> None:
            messages.append(message)

        await self.app(scope, receive, capture)
        start = next(
            (message for message in messages if message["type"] == "http.response.start"),
            None,
        )
        if start is not None and 200 <= start["status"] < 300 and legacy_candidate:
            user_agent = self._sanitized_user_agent(scope)
            logger.info(
                "mcp_legacy_protocol_request_accepted "
                "protocol_era=legacy user_agent=%r",
                user_agent,
                extra={
                    "event": "mcp_legacy_protocol_request_accepted",
                    "protocol_era": "legacy",
                    "user_agent": user_agent,
                },
            )
        if start is not None and start["status"] in {401, 403}:
            headers = {
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in start.get("headers", [])
                if name.lower() not in {b"content-length", b"content-type"}
            }
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "Authentication required"},
                },
                status_code=start["status"],
                headers=headers,
            )
            await response(scope, receive, send)
            return
        for message in messages:
            await send(message)

    @staticmethod
    def _sanitized_user_agent(scope: Scope) -> str:
        raw = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == b"user-agent"
            ),
            "",
        )
        printable = "".join(character if character.isprintable() else "?" for character in raw)
        return printable[:LEGACY_USER_AGENT_MAX_LENGTH]

    @staticmethod
    async def _jsonrpc_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        code: int,
        message: str,
        *,
        data: dict[str, object] | None = None,
    ) -> None:
        error: dict[str, object] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        response = JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": error},
            status_code=400,
        )
        await response(scope, receive, send)


class MCPPathRouter:
    """Route the public well-known MCP without weakening the product server."""

    def __init__(self, *, protected: ASGIApp, public: ASGIApp) -> None:
        self.protected = protected
        self.public = public

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = self.public if scope.get("path") == PUBLIC_MCP_PATH else self.protected
        await app(scope, receive, send)
