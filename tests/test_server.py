"""Tests for `spekoai_mcp.server`.

The server exposes public knowledge surfaces (resources, prompts,
`search_docs`, `list_packages`) plus identity-aware action tools
(`get_balance`). The OAuth plumbing (`auth.py`, the `auth=` kwarg on
`create_server`, the CLI env-var handling in `__main__.py`) is needed
for the action tools and retained end-to-end. See `test_auth.py` for
the OAuth-wiring tests.
"""

from __future__ import annotations

from spekoai_mcp.server import create_server


async def test_create_server_without_auth() -> None:
    mcp = create_server()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {"search_docs", "list_packages", "get_balance"}


async def test_resources_and_prompts_advertised() -> None:
    """The knowledge-layer surfaces (static docs + scaffolding prompt)
    must show up on a bare `create_server()` — they don't depend on
    auth or runtime config."""
    mcp = create_server()
    resources = await mcp.list_resources()
    assert any(str(r.uri) == "spekoai://docs/index" for r in resources)
    templates = await mcp.list_resource_templates()
    assert any(t.uri_template == "spekoai://docs/{slug}" for t in templates)
    prompts = await mcp.list_prompts()
    assert any(p.name == "scaffold_project" for p in prompts)


async def test_list_packages_returns_structured_manifest() -> None:
    mcp = create_server()
    result = await mcp._call_tool_mcp("list_packages", {})  # type: ignore[attr-defined]
    # `structuredContent` from FastMCP wraps the return value under a
    # `result` key; the tool returns `list[PackageInfo]`.
    payload = result.structuredContent or {}
    rows = payload.get("result", [])
    assert rows, "list_packages returned no rows"
    names = {row["package_name"] for row in rows}
    assert "@spekoai/sdk" in names
    assert "@spekoai/client" in names
    assert "@spekoai/adapter-livekit" in names
    # Internal packages must NOT be exposed — `@spekoai/core` and
    # `@spekoai/providers` are `"private": true` and their docs are
    # deliberately not bundled into the public MCP.
    assert "@spekoai/core" not in names
    assert "@spekoai/providers" not in names
