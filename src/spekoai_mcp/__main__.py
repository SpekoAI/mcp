"""CLI entrypoint: HTTP server with OAuth-forwarded auth."""

from __future__ import annotations

import argparse
import sys

from spekoai_mcp.auth import build_auth
from spekoai_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="spekoai-mcp", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080).")
    args = parser.parse_args()

    auth = build_auth()
    if auth is None:
        sys.exit(
            "error: OAuth is required. Set SPEKOAI_OAUTH_ISSUER, "
            "SPEKOAI_OAUTH_CLIENT_ID, SPEKOAI_OAUTH_CLIENT_SECRET, and "
            "SPEKOAI_MCP_BASE_URL."
        )

    mcp = create_server(auth=auth)
    # Trust X-Forwarded-* from the Cloud Run / load-balancer fronting the
    # container. Without this, Starlette sees scheme=http on redirects and
    # emits `Location: http://...` — any client following the redirect with
    # its bearer token leaks the token over cleartext.
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        uvicorn_config={"proxy_headers": True, "forwarded_allow_ips": "*"},
    )


if __name__ == "__main__":
    main()
