"""Docs self-serve tools for the hosted MCP server.

Re-registered after PR #316 collapsed the split public/private servers
into one authenticated `/mcp` endpoint: the single-endpoint decision
stands, but agents still need an in-band way to look up correct request
shapes when a write-tool body is rejected. `search_docs` plus the
`spekoai://docs/*` resources (see `resources.py`) restore that without
reintroducing an unauthenticated surface.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from spekoai_mcp import search

DOCS_TOOL_NAMES = [
    "search_docs",
]


def register_docs_tools(mcp: FastMCP) -> None:
    for tool in [
        search_docs,
    ]:
        mcp.tool(tool)


async def search_docs(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text query. Matched case-insensitively against the "
                "titles and bodies of every bundled Speko doc (hosted "
                "llms.txt exports, SDK/adapter READMEs, migration guides, "
                "quickstart)."
            ),
        ),
    ],
    limit: Annotated[
        int,
        Field(ge=1, le=20, description="Max hits to return. Defaults to 5."),
    ] = 5,
) -> list[search.DocHit]:
    """Search bundled Speko docs. Returns slug, title, score, snippet.

    Use this to look up SDK usage, API request/body shapes, and migration
    steps without leaving the MCP session. For example, after a Speko API
    validation error, search the failing field name to find the correct
    shape. Each hit's `slug` can be opened as the `spekoai://docs/{slug}`
    resource; `spekoai://docs/index` lists every bundled doc.
    """
    return search.search(query, limit=limit)
