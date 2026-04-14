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

# Sibling sdk-python is path-sourced via tool.uv.sources, so the build
# context must include it. docker-bake.hcl sets context=../.. accordingly.
COPY packages/mcp-server packages/mcp-server
COPY packages/sdk-python packages/sdk-python

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
