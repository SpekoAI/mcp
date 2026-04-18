# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy
WORKDIR /app

RUN pip install --no-cache-dir uv

# ---- Build stage: install deps into .venv ----
FROM base AS build

# Sibling packages supply the README/SKILLS files that
# scripts/sync_docs.py bundles into spekoai_mcp/_docs/. Only PUBLIC
# packages are copied in — `packages/core`, `packages/providers`, and
# the root CLAUDE.md are intentionally NOT COPY'd because they contain
# internal architecture details (private packages with `"private":
# true`, references to `apps/*`) we don't want bundled into a
# publicly-reachable MCP server.
#
# docker-bake.hcl sets context=../.. so these paths resolve.
# (sdk-python is bundled for its docs too; when we re-add the `spekoai`
# runtime dep it's already path-sourceable via tool.uv.sources.)
COPY packages/mcp-server packages/mcp-server
COPY packages/sdk-python packages/sdk-python
COPY packages/sdk packages/sdk
COPY packages/client packages/client
COPY packages/adapter-livekit packages/adapter-livekit
COPY packages/adapter-vapi packages/adapter-vapi
COPY packages/adapter-retell packages/adapter-retell

# Generate _docs/ inside the build stage so the wheel always ships
# docs in sync with the sibling packages in this build context —
# never fall back to stale files a dev might have committed.
RUN cd packages/mcp-server && python scripts/sync_docs.py

RUN --mount=type=cache,target=/root/.cache/uv \
    cd packages/mcp-server && uv sync --frozen --no-dev

# ---- Runtime image ----
FROM base AS production

# Run as a non-root user. uid/gid are fixed so bind-mounted volumes have
# predictable ownership across hosts.
RUN groupadd --system --gid 1001 spekoai \
 && useradd --system --uid 1001 --gid spekoai --home-dir /app --shell /usr/sbin/nologin spekoai

COPY --from=build --chown=spekoai:spekoai /app/packages/sdk-python packages/sdk-python
COPY --from=build --chown=spekoai:spekoai /app/packages/mcp-server packages/mcp-server

# FastMCP's OAuthProxy persists DCR client records under `FASTMCP_HOME`.
# Default is `platformdirs.user_data_dir("fastmcp")` which needs $HOME and a
# writable ~/.local tree — neither exists for this non-root user, so mkdir
# recurses. Pin to a pre-created, owned path instead.
ENV FASTMCP_HOME=/app/.fastmcp \
    HOME=/app
RUN mkdir -p /app/.fastmcp && chown -R spekoai:spekoai /app/.fastmcp

WORKDIR /app/packages/mcp-server
ENV PATH="/app/packages/mcp-server/.venv/bin:${PATH}"
EXPOSE 8080

USER spekoai

# No HEALTHCHECK — Cloud Run ignores Dockerfile healthchecks and drives
# its own probes against /health via the service config.

CMD ["spekoai-mcp"]
