"""Components are not advertised by the hosted MCP server in this pass.

Docs resources (`spekoai://docs/...`) ARE advertised (see
`test_resources.py`), so this guard only checks the component namespace.
"""

from __future__ import annotations

from spekoai_mcp.server import create_server


async def test_component_resources_not_advertised() -> None:
    mcp = create_server()
    uris = [str(resource.uri) for resource in await mcp.list_resources()]
    assert not any(uri.startswith("spekoai://components/") for uri in uris)
