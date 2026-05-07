"""mcp-caddy: Caddy web server management MCP server."""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("caddy")

_DEFAULT_HOST = "http://localhost:2019"
_DEFAULT_TIMEOUT = 30.0


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Make an HTTP request to Caddy admin API.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, etc.)
        path: API path (e.g., "/config/")
        **kwargs: Additional arguments to pass to httpx.AsyncClient.request()

    Returns:
        httpx.Response object

    Environment variables:
        CADDY_HOST: Caddy admin API host (default: http://localhost:2019)
        CADDY_TIMEOUT: Request timeout in seconds (default: 30.0)
    """
    host = os.environ.get("CADDY_HOST", _DEFAULT_HOST)
    timeout = float(os.environ.get("CADDY_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, f"{host}{path}", **kwargs)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
