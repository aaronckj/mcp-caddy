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


def _extract_upstreams(handles: list) -> list[str]:
    """Extract upstream dial addresses, recursing into subroute handlers."""
    upstreams = []
    for handle in handles:
        if handle.get("handler") == "reverse_proxy":
            for up in handle.get("upstreams", []):
                if "dial" in up:
                    upstreams.append(up["dial"])
        elif handle.get("handler") == "subroute":
            for sub_route in handle.get("routes", []):
                upstreams.extend(_extract_upstreams(sub_route.get("handle", [])))
    return upstreams


@mcp.tool()
async def list_routes() -> dict:
    """List all configured routes (virtual hosts) parsed from Caddy's HTTP app config."""
    try:
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        config = resp.json()

        servers = config.get("apps", {}).get("http", {}).get("servers", {})
        routes_out = []

        for server_name, server in servers.items():
            for route in server.get("routes", []):
                hosts = []
                for match in route.get("match", []):
                    hosts.extend(match.get("host", []))

                handles = route.get("handle", [])
                handler_type = handles[0].get("handler") if handles else None
                upstreams = _extract_upstreams(handles)

                routes_out.append({
                    "server": server_name,
                    "hosts": hosts,
                    "handler": handler_type,
                    "upstreams": upstreams,
                })

        return {"result": routes_out}
    except Exception as e:
        return {"error": str(e), "tool": "list_routes", "detail": type(e).__name__}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
