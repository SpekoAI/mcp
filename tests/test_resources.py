"""Resource surface tests.

Every advertised slug must resolve to non-empty markdown, the index must
list every slug, and unknown slugs must raise `ResourceError`.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ResourceError

from spekoai_mcp.docs import all_slugs, format_index, load_manifest, read_doc
from spekoai_mcp.server import create_server


def test_every_slug_resolves() -> None:
    for slug in all_slugs():
        body = read_doc(slug)
        assert body.strip(), f"doc {slug!r} is empty"


def test_index_lists_every_slug() -> None:
    index = format_index()
    for slug in all_slugs():
        uri = f"spekoai://docs/{slug}"
        assert uri in index, f"index missing {uri}"


def test_manifest_entries_have_required_fields() -> None:
    required = {"slug", "source", "package_name", "status", "kind", "title", "summary"}
    for entry in load_manifest():
        missing = required - set(entry.keys())
        assert not missing, f"{entry['slug']} missing {missing}"


def test_no_internal_docs_are_bundled() -> None:
    """Prevent regression: internal packages, CLAUDE.md, and roadmaps
    must never end up in a public MCP deployment."""
    slugs = set(all_slugs())
    forbidden = {
        "core-readme", "core-skills",
        "providers-readme", "providers-skills",
        "platform-claude-md",
        "client-roadmap", "mcp-server-roadmap",
    }
    leaked = slugs & forbidden
    assert not leaked, f"internal docs leaked into bundle: {leaked}"
    # Also check no slug's `status` is "internal" — structural guard.
    for entry in load_manifest():
        assert entry["status"] != "internal", (
            f"internal-status doc {entry['slug']!r} made it into the "
            "bundled manifest; it should have been excluded in "
            "sync_docs.py"
        )


async def test_server_advertises_resources_and_template() -> None:
    mcp = create_server()
    resources = await mcp.list_resources()
    assert any(str(r.uri) == "spekoai://docs/index" for r in resources)
    templates = await mcp.list_resource_templates()
    assert any(t.uri_template == "spekoai://docs/{slug}" for t in templates)


async def test_reading_index_returns_markdown() -> None:
    mcp = create_server()
    result = await mcp.read_resource("spekoai://docs/index")
    content = result.contents[0]
    assert content.content.startswith("# SpekoAI documentation index")


async def test_reading_each_slug_returns_body() -> None:
    mcp = create_server()
    for slug in all_slugs():
        result = await mcp.read_resource(f"spekoai://docs/{slug}")
        body = result.contents[0].content
        assert isinstance(body, str)
        assert body.strip()


async def test_unknown_slug_raises_resource_error() -> None:
    mcp = create_server()
    with pytest.raises(ResourceError, match="unknown doc slug"):
        await mcp.read_resource("spekoai://docs/definitely-not-a-real-slug")
