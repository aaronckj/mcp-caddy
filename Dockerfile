FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN uv pip install --system .

# mcp-caddy speaks MCP over stdio; there is no HTTP listener to health-check.
ENTRYPOINT ["mcp-caddy"]
