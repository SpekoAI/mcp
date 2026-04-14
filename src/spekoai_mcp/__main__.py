"""CLI entrypoint: HTTP (default, OAuth-protected) or stdio (local dev)."""

from __future__ import annotations

import argparse
import os
import sys

from spekoai_mcp.auth import build_auth
from spekoai_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="spekoai-mcp", description=__doc__)
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run over stdio (local development; no OAuth, uses SPEKOAI_API_KEY).",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (default: 8080).")
    args = parser.parse_args()

    if not os.environ.get("SPEKOAI_API_KEY"):
        sys.exit(
            "error: SPEKOAI_API_KEY is required. Set it in the environment before "
            "starting the server."
        )

    if args.stdio:
        mcp = create_server()
        mcp.run(transport="stdio")
        return

    auth = build_auth()
    if auth is None:
        sys.exit(
            "error: HTTP mode requires SPEKOAI_OAUTH_ISSUER, SPEKOAI_OAUTH_CLIENT_ID, "
            "and SPEKOAI_OAUTH_CLIENT_SECRET. Use --stdio for local development "
            "without OAuth."
        )

    mcp = create_server(auth=auth)
    mcp.run(transport="http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
