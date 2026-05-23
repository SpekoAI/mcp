"""Resource surface tests.

Every advertised slug must resolve to non-empty markdown, the index must
list every slug, and unknown slugs must raise `ResourceError`.
"""

from __future__ import annotations

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


def test_migration_guides_are_bundled() -> None:
    slugs = set(all_slugs())
    for platform in ["livekit", "pipecat", "retell", "vapi"]:
        slug = f"migration-{platform}"
        assert slug in slugs
        body = read_doc(slug)
        assert platform in body.lower()
        assert "parse_external_config" in body


def test_llms_resources_are_bundled() -> None:
    slugs = set(all_slugs())
    assert {"llms", "llms-full"} <= slugs
    assert "VoiceConversation" in read_doc("llms-full")
    assert "createSpekoComponents" in read_doc("llms-full")


def test_no_internal_docs_are_bundled() -> None:
    """Prevent regression: internal packages, CLAUDE.md, and roadmaps
    must never end up in a public MCP deployment."""
    slugs = set(all_slugs())
    forbidden = {
        "core-readme",
        "providers-readme",
        "sdk-skills",
        "client-skills",
        "sdk-python-skills",
        "adapter-livekit-skills",
        "adapter-vapi-skills",
        "adapter-retell-skills",
        "platform-claude-md",
        "client-roadmap",
        "mcp-server-roadmap",
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


async def test_server_does_not_advertise_resources_or_templates() -> None:
    mcp = create_server()
    assert await mcp.list_resources() == []
    assert await mcp.list_resource_templates() == []
