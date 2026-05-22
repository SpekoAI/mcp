"""Components are not advertised by the hosted MCP server in this pass."""

from __future__ import annotations

from spekoai_mcp.server import create_server


async def test_component_resources_not_advertised() -> None:
    mcp = create_server()
    assert await mcp.list_resources() == []
