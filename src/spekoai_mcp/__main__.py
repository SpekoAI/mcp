"""CLI entrypoint for the hosted HTTP MCP server.

The server exposes one protected MCP endpoint at `/mcp`. It accepts OAuth
bearer tokens when OAuth env vars are configured, and always accepts Speko API
keys as `Authorization: Bearer sk_*`.
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from spekoai_mcp.auth import build_auth
from spekoai_mcp.server import MCP_PATH, create_app

logger = logging.getLogger("spekoai_mcp")


def main() -> None:
    parser = argparse.ArgumentParser(prog="spekoai-mcp", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080).")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    auth = build_auth(mcp_path=MCP_PATH)
    if auth.server is None:
        logger.info("spekoai-mcp: running API-key protected MCP at %s.", MCP_PATH)
    else:
        logger.info("spekoai-mcp: running OAuth/API-key protected MCP at %s.", MCP_PATH)

    app = create_app(auth=auth)
    # Trust X-Forwarded-* from the Cloud Run / load-balancer fronting
    # the container. Without this, Starlette sees scheme=http on
    # redirects and emits `Location: http://...` — any client following
    # the redirect with a bearer token would leak it over cleartext.
    # Safe to keep in public mode too; it just doesn't matter when no
    # token is involved.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        lifespan="on",
        timeout_graceful_shutdown=2,
        ws="websockets-sansio",
    )


if __name__ == "__main__":
    main()
