"""FastMCP v3 server exposing SpekoAI knowledge to MCP clients.

Surfaces:

- **Resources** — `spekoai://docs/index` and `spekoai://docs/{slug}` ship
  every SDK/adapter's README + SKILLS.md + CLAUDE.md + a quickstart
  example inside the wheel. See `docs.py`, `resources.py`.
- **Prompts** — `scaffold_project` (scenario, language, runtime) walks
  an MCP client through bootstrapping a SpekoAI project. See `prompts.py`.
- **Tools** — `search_docs` (full-text over bundled docs) and
  `list_packages` (structured manifest).

Auth model: today every surface ships static bundled data, so OAuth is
not required to use this server. The wiring is retained end-to-end
(`auth.py` builds an `OAuthProxy`; `create_server(auth=)` accepts one;
the CLI mounts it if env vars are present) — when future tools need
the caller's identity they can declare `auth=` per-component and the
deployment flips OAuth env vars on. Do not remove the auth plumbing
to simplify; it's intentionally future-ready.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from fastmcp.server.auth import OAuthProxy
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from spekoai_mcp import search
from spekoai_mcp.docs import DocEntry, load_manifest
from spekoai_mcp.prompts import register_prompts
from spekoai_mcp.resources import register_resources

INSTRUCTIONS = "\n\n".join(
    " ".join(paragraph.split())
    for paragraph in [
        """
        SpekoAI MCP — the authoritative source for SpekoAI's SDKs,
        adapters, and platform.
        """,
        """
        Start here: call the `scaffold_project` prompt to bootstrap a
        new project, or read the `spekoai://docs/index` resource to see
        every bundled doc. Prefer the skill-sheet resources
        (`spekoai://docs/*-skills`) first — they are dense,
        LLM-oriented summaries of each package's API, gotchas, and
        minimal snippets. READMEs (`spekoai://docs/*-readme`) are longer
        prose. Use the `search_docs(query)` tool when you need to find
        something across all bundled docs, and `list_packages()` for
        structured metadata.
        """,
        """
        SpekoAI is a voice-AI gateway: one API that routes STT, LLM,
        and TTS calls to the best provider per (language, vertical,
        optimizeFor), with failover handled server-side. Public
        packages: `@spekoai/sdk` (TypeScript server SDK),
        `@spekoai/client` (browser WebRTC SDK), `spekoai` (Python
        server SDK), `@spekoai/adapter-livekit` (LiveKit Agents
        wrapper). `@spekoai/adapter-vapi` and `@spekoai/adapter-retell`
        are scaffolded placeholders — not production-ready.
        """,
    ]
)


class PackageInfo(BaseModel):
    """Structured package metadata returned by `list_packages`."""

    package_name: str = Field(description="Human name or npm/PyPI name.")
    npm_or_pypi: str | None = Field(
        description="Canonical install name, or null for internal packages."
    )
    status: str = Field(description="One of: stable, alpha, scaffold, internal, platform.")
    readme_uri: str | None = Field(
        description="MCP resource URI for the README, if one is shipped."
    )
    skills_uri: str | None = Field(
        description="MCP resource URI for the SKILLS sheet, if one is shipped."
    )


def _build_package_infos(manifest: list[DocEntry]) -> list[PackageInfo]:
    """Collapse per-doc manifest entries into one row per package."""
    by_name: dict[str, dict[str, object]] = {}
    for entry in manifest:
        name = entry["package_name"]
        row = by_name.setdefault(
            name,
            {
                "package_name": name,
                "npm_or_pypi": entry["npm_or_pypi"],
                "status": entry["status"],
                "readme_uri": None,
                "skills_uri": None,
            },
        )
        uri = f"spekoai://docs/{entry['slug']}"
        if entry["kind"] == "readme":
            row["readme_uri"] = uri
        elif entry["kind"] == "skills":
            row["skills_uri"] = uri
    return [PackageInfo(**row) for row in by_name.values()]  # type: ignore[arg-type]


def create_server(auth: OAuthProxy | None = None) -> FastMCP:
    """Build a FastMCP server. `auth` is passed via the constructor (the
    supported wiring path); production deployments must supply one.
    """
    mcp: FastMCP = FastMCP(
        name="spekoai",
        instructions=INSTRUCTIONS,
        auth=auth,
    )

    register_resources(mcp)
    register_prompts(mcp)

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @mcp.tool
    async def search_docs(
        _ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "Free-text query. Matched case-insensitively against "
                    "titles and body of every bundled SpekoAI doc."
                ),
            ),
        ],
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=20,
                description="Max hits to return. Defaults to 5.",
            ),
        ] = 5,
    ) -> list[search.DocHit]:
        """Search bundled SpekoAI docs. Returns slug, title, score, snippet.

        Use this when you need to find something across all SDKs/adapters
        without reading every resource. Each hit's `slug` can be opened
        directly as `spekoai://docs/{slug}`.
        """
        return search.search(query, limit=limit)

    @mcp.tool
    async def list_packages(_ctx: Context) -> list[PackageInfo]:
        """List every SpekoAI package known to this server, with URIs to
        the bundled README and SKILLS sheet where available.

        Prefer this over parsing markdown when you want structured data
        (e.g. "which packages are production-stable vs scaffold-only?").
        """
        return _build_package_infos(load_manifest())

    return mcp
