"""CLI entrypoint.

OAuth is optional: if the four `SPEKOAI_OAUTH_*` env vars are set the
server mounts `OAuthProxy` and exposes the `/auth/*` + OIDC discovery
routes, ready for future auth-gated tools. With no env vars set the
server runs public — fine today because every registered surface ships
static bundled data. Keep the wiring so flipping OAuth back on is an
env-config change, not a code change.
"""

from __future__ import annotations

import argparse
import logging

from spekoai_mcp.auth import build_auth
from spekoai_mcp.server import create_server

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

    auth = build_auth()
    if auth is None:
        logger.info(
            "spekoai-mcp: running public (no OAuth env vars set). All "
            "registered surfaces today are public-safe static data."
        )
    else:
        logger.info("spekoai-mcp: OAuth mounted; JWTs verified via OAuthProxy.")

    mcp = create_server(auth=auth)
    # Trust X-Forwarded-* from the Cloud Run / load-balancer fronting
    # the container. Without this, Starlette sees scheme=http on
    # redirects and emits `Location: http://...` — any client following
    # the redirect with a bearer token would leak it over cleartext.
    # Safe to keep in public mode too; it just doesn't matter when no
    # token is involved.
    mcp.run(
        transport="http",
        host=args.host,
        port=args.port,
        uvicorn_config={"proxy_headers": True, "forwarded_allow_ips": "*"},
    )


if __name__ == "__main__":
    main()
