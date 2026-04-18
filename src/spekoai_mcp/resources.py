"""MCP resources that expose the bundled SpekoAI docs.

Two resources are registered:

- `spekoai://docs/index` — non-parameterized; lists every bundled slug
  with a one-line summary so a client can discover the surface cheaply.
- `spekoai://docs/{slug}` — parameterized; returns the markdown body
  for one slug. Unknown slugs raise a `ToolError` with the available
  slugs so the client can self-correct.
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError

from spekoai_mcp.docs import all_slugs, format_index, read_doc


def register_resources(mcp: FastMCP) -> None:
    @mcp.resource(
        "spekoai://docs/index",
        name="docs_index",
        title="SpekoAI documentation index",
        description=(
            "Index of every bundled SpekoAI doc: SDKs, adapters, "
            "platform overview, quickstart. Read this first to find "
            "the right resource for any SpekoAI question."
        ),
        mime_type="text/markdown",
    )
    def docs_index() -> str:
        return format_index()

    @mcp.resource(
        "spekoai://docs/{slug}",
        name="docs",
        description=(
            "SpekoAI documentation by slug. Call spekoai://docs/index "
            "for the list of valid slugs."
        ),
        mime_type="text/markdown",
    )
    def doc(slug: str) -> str:
        try:
            return read_doc(slug)
        except KeyError:
            slugs = ", ".join(all_slugs())
            raise ResourceError(
                f"unknown doc slug: {slug!r}. Valid slugs: {slugs}"
            ) from None
