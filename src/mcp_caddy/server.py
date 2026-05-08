"""mcp-caddy: Caddy web server management MCP server."""

from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("caddy")

_DEFAULT_HOST = "http://localhost:2019"
_DEFAULT_TIMEOUT = 30.0


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    host = os.environ.get("CADDY_HOST", _DEFAULT_HOST)
    timeout = float(os.environ.get("CADDY_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method, f"{host}{path}", **kwargs)


def _err(e: Exception, tool: str) -> dict:
    out: dict = {"error": str(e), "tool": tool, "detail": type(e).__name__}
    if isinstance(e, httpx.HTTPStatusError):
        out["status"] = e.response.status_code
        try:
            out["body"] = e.response.json()
        except Exception:
            out["body"] = e.response.text[:500]
    return out


@mcp.tool()
async def server_info() -> dict:
    """Get Caddy server version and list of loaded modules."""
    try:
        resp = await _request("GET", "/")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "server_info")


@mcp.tool()
async def get_config() -> dict:
    """Get the full Caddy configuration as JSON."""
    try:
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_config")


@mcp.tool()
async def get_config_path(config_path: str) -> dict:
    """Get a specific Caddy config node by path. config_path: e.g. '/apps/http/servers' or '/apps/tls'."""
    if not config_path.startswith("/"):
        return {"error": "config_path must start with '/'", "tool": "get_config_path"}
    try:
        resp = await _request("GET", f"/config{config_path}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_config_path")


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
    """List all configured routes (virtual hosts) with hosts, handler, upstreams, and listen addresses."""
    try:
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        config = resp.json()

        servers = config.get("apps", {}).get("http", {}).get("servers", {})
        routes_out = []

        for server_name, server in servers.items():
            listen = server.get("listen", [])
            for idx, route in enumerate(server.get("routes", [])):
                hosts = []
                for match in route.get("match", []):
                    hosts.extend(match.get("host", []))

                handles = route.get("handle", [])
                handler_type = handles[0].get("handler") if handles else None
                upstreams = _extract_upstreams(handles)

                routes_out.append({
                    "server": server_name,
                    "index": idx,
                    "listen": listen,
                    "hosts": hosts,
                    "handler": handler_type,
                    "upstreams": upstreams,
                })

        return {"result": routes_out}
    except Exception as e:
        return _err(e, "list_routes")


@mcp.tool()
async def add_reverse_proxy_route(host: str, upstream: str, server_name: str = "") -> dict:
    """Add a reverse proxy route to Caddy. host: domain (e.g. 'app.example.com'). upstream: backend dial address (e.g. 'localhost:3000'). server_name: Caddy server block (auto-detects first server if empty)."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_reverse_proxy_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_reverse_proxy_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/")
            resp.raise_for_status()
            config = resp.json() or {}
            servers = config.get("apps", {}).get("http", {}).get("servers", {})
            if not servers:
                return {
                    "error": "No HTTP servers found in Caddy config. Use reload to load an initial configuration first.",
                    "tool": "add_reverse_proxy_route",
                }
            server_name = next(iter(servers))

        route = {
            "match": [{"host": [host]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        resp = await _request("POST", f"/config/apps/http/servers/{server_name}/routes", json=route)
        resp.raise_for_status()
        return {"result": {"added": True, "host": host, "upstream": upstream, "server": server_name}}
    except Exception as e:
        return _err(e, "add_reverse_proxy_route")


@mcp.tool()
async def add_static_file_server(path: str, root: str, server_name: str = "") -> dict:
    """Add a static file server route to Caddy. path: URL path to match (e.g., '/files/*'). root: filesystem directory to serve. server_name: auto-detects first server if empty."""
    if not path or not path.strip():
        return {"error": "path must not be empty", "tool": "add_static_file_server"}
    if not root or not root.strip():
        return {"error": "root must not be empty", "tool": "add_static_file_server"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/")
            resp.raise_for_status()
            config = resp.json() or {}
            servers = config.get("apps", {}).get("http", {}).get("servers", {})
            if not servers:
                return {"error": "No HTTP servers configured in Caddy", "tool": "add_static_file_server"}
            server_name = next(iter(servers))

        route = {
            "match": [{"path": [path]}],
            "handle": [{"handler": "file_server", "root": root}],
        }
        resp = await _request("POST", f"/config/apps/http/servers/{server_name}/routes", json=route)
        resp.raise_for_status()
        return {"result": {"added": True, "server": server_name, "path": path, "root": root}}
    except Exception as e:
        return _err(e, "add_static_file_server")


@mcp.tool()
async def delete_route(server_name: str, route_index: int) -> dict:
    """Delete a route by index from an HTTP server's route list. Use list_routes to find the index. Changes take effect immediately."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "delete_route"}
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "delete_route"}
    try:
        resp = await _request("DELETE", f"/config/apps/http/servers/{server_name}/routes/{route_index}")
        resp.raise_for_status()
        return {"result": {"server_name": server_name, "route_index": route_index, "deleted": True}}
    except Exception as e:
        return _err(e, "delete_route")


@mcp.tool()
async def list_upstreams() -> dict:
    """List all reverse proxy upstreams with their health status and request counts."""
    try:
        resp = await _request("GET", "/reverse_proxy/upstreams")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_upstreams")


@mcp.tool()
async def get_certificates() -> dict:
    """List TLS certificate automation policies: subjects, ACME issuers, and CAs."""
    try:
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        config = resp.json()

        policies = (
            config.get("apps", {})
            .get("tls", {})
            .get("automation", {})
            .get("policies", [])
        )

        certs = []
        for policy in policies:
            issuers = []
            for issuer in policy.get("issuers", []):
                issuer_info: dict = {"module": issuer.get("module")}
                if "ca" in issuer:
                    issuer_info["ca"] = issuer["ca"]
                if "email" in issuer:
                    issuer_info["email"] = issuer["email"]
                issuers.append(issuer_info)

            certs.append({
                "subjects": policy.get("subjects", []),
                "issuers": issuers,
            })

        return {"result": certs}
    except Exception as e:
        return _err(e, "get_certificates")


@mcp.tool()
async def adapt_config(caddyfile: str) -> dict:
    """Convert a Caddyfile snippet to JSON config using Caddy's built-in adapter."""
    if not caddyfile or not caddyfile.strip():
        return {"error": "caddyfile must not be empty", "tool": "adapt_config"}
    try:
        resp = await _request(
            "POST",
            "/adapt",
            content=caddyfile.encode(),
            params={"adapter": "caddyfile"},
            headers={"Content-Type": "text/caddyfile"},
        )
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "adapt_config")


@mcp.tool()
async def reload(source: str) -> dict:
    """Reload Caddy config. source: raw JSON string (starts with '{') or path to a JSON config file."""
    try:
        if source.lstrip().startswith("{"):
            config_data = json.loads(source)
        else:
            if not os.path.exists(source):
                return {"error": f"Config file not found: {source}", "tool": "reload"}
            with open(source) as f:
                config_data = json.load(f)

        resp = await _request("POST", "/load", json=config_data)
        resp.raise_for_status()
        return {"result": {"reloaded": True}}
    except Exception as e:
        return _err(e, "reload")


@mcp.tool()
async def update_config_path(config_path: str, value: str) -> dict:
    """Update a specific Caddy config path with a new value via PATCH. config_path: e.g. '/apps/http/servers/srv0/listen'. value: JSON string of the new value."""
    if not config_path.startswith("/"):
        return {"error": "config_path must start with '/'", "tool": "update_config_path"}
    try:
        new_value = json.loads(value)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON value: {e}", "tool": "update_config_path"}
    try:
        resp = await _request("PATCH", f"/config{config_path}", json=new_value)
        resp.raise_for_status()
        return {"result": {"updated": True, "path": config_path}}
    except Exception as e:
        return _err(e, "update_config_path")


@mcp.tool()
async def delete_config_path(config_path: str) -> dict:
    """Delete a specific Caddy config node at the given path. config_path: e.g. '/apps/http/servers/srv0'. Changes take effect immediately."""
    if not config_path.startswith("/"):
        return {"error": "config_path must start with '/'", "tool": "delete_config_path"}
    try:
        resp = await _request("DELETE", f"/config{config_path}")
        resp.raise_for_status()
        return {"result": {"deleted": True, "path": config_path}}
    except Exception as e:
        return _err(e, "delete_config_path")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
