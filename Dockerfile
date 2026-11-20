# graphify-hub: multi-project Graphify MCP server (streamable HTTP on :3000/mcp).
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN useradd --create-home --uid 10001 hub && mkdir /data && chown hub:hub /data
USER hub

ENV HUB_DATA_DIR=/data \
    HOME=/tmp \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=3000
EXPOSE 3000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 3000), 3)"

ENTRYPOINT ["graphify-hub"]
CMD ["--mode", "http"]
