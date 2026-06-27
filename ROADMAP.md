# MCP Server Roadmap

## Planned

- **Shared `OAuthProxy` client storage for multi-replica deploys.** Current
  deploy persists DCR client records to the container filesystem
  (`FASTMCP_HOME=/app/.fastmcp`), so clients registered against one replica
  don't exist on others. Swap for a shared `AsyncKeyValue` backend (Redis is
  the FastMCP-recommended option) passed as `client_storage=` to
  `OAuthProxy(...)` in `src/spekoai_mcp/auth.py`.
