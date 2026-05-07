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


@mcp.tool()
async def server_info() -> dict:
    """Get Caddy server version and list of loaded modules."""
    try:
        resp = await _request("GET", "/")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "server_info", "detail": type(e).__name__}


@mcp.tool()
async def get_config() -> dict:
    """Get the full Caddy configuration as JSON."""
    try:
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "get_config", "detail": type(e).__name__}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
