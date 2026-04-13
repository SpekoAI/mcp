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

COPY --from=build /app/packages/sdk-python packages/sdk-python
COPY --from=build /app/packages/mcp-server packages/mcp-server

WORKDIR /app/packages/mcp-server
ENV PATH="/app/packages/mcp-server/.venv/bin:${PATH}"
EXPOSE 8080

CMD ["spekoai-mcp"]
