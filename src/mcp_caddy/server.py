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
        return {"error": str(e), "tool": "get_config_path", "detail": type(e).__name__}


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
            for route in server.get("routes", []):
                hosts = []
                for match in route.get("match", []):
                    hosts.extend(match.get("host", []))

                handles = route.get("handle", [])
                handler_type = handles[0].get("handler") if handles else None
                upstreams = _extract_upstreams(handles)

                routes_out.append({
                    "server": server_name,
                    "listen": listen,
                    "hosts": hosts,
                    "handler": handler_type,
                    "upstreams": upstreams,
                })

        return {"result": routes_out}
    except Exception as e:
        return {"error": str(e), "tool": "list_routes", "detail": type(e).__name__}


@mcp.tool()
async def list_upstreams() -> dict:
    """List all reverse proxy upstreams with their health status and request counts."""
    try:
        resp = await _request("GET", "/reverse_proxy/upstreams")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return {"error": str(e), "tool": "list_upstreams", "detail": type(e).__name__}


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
        return {"error": str(e), "tool": "get_certificates", "detail": type(e).__name__}


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
        return {"error": str(e), "tool": "adapt_config", "detail": type(e).__name__}


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
        return {"error": str(e), "tool": "reload", "detail": type(e).__name__}


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
        return {"error": str(e), "tool": "update_config_path", "detail": type(e).__name__}


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
        return {"error": str(e), "tool": "delete_config_path", "detail": type(e).__name__}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
