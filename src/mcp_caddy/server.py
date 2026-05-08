"""mcp-caddy: Caddy web server management MCP server."""

from __future__ import annotations

import ipaddress
import json
import os
import re

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
        try:
            return {"result": resp.json()}
        except Exception:
            return {"result": {"raw": resp.text}}
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
    if not config_path or not config_path.strip():
        return {"error": "config_path must not be empty", "tool": "get_config_path"}
    config_path = config_path.strip()
    if not config_path.startswith("/"):
        return {"error": "config_path must start with '/'", "tool": "get_config_path"}
    try:
        resp = await _request("GET", f"/config{config_path}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_config_path"); err["config_path"] = config_path; return err


@mcp.tool()
async def list_servers() -> dict:
    """List all Caddy HTTP server blocks with their names, listen addresses, and route counts."""
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        return {
            "result": [
                {
                    "name": name,
                    "listen": cfg.get("listen", []),
                    "route_count": len(cfg.get("routes", [])),
                }
                for name, cfg in servers.items()
            ]
        }
    except Exception as e:
        return _err(e, "list_servers")


@mcp.tool()
async def get_server(server_name: str) -> dict:
    """Get full configuration of a Caddy HTTP server block including listen addresses, routes, and TLS settings."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "get_server"}
    server_name = server_name.strip()
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_server"); err["server_name"] = server_name; return err


@mcp.tool()
async def delete_server(server_name: str) -> dict:
    """Delete a Caddy HTTP server block entirely, removing all its routes and configuration. This is irreversible without a config backup."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "delete_server"}
    server_name = server_name.strip()
    try:
        resp = await _request("DELETE", f"/config/apps/http/servers/{server_name}")
        resp.raise_for_status()
        return {"result": {"deleted": True, "name": server_name}}
    except Exception as e:
        err = _err(e, "delete_server"); err["server_name"] = server_name; return err


@mcp.tool()
async def create_server(server_name: str, listen: str = ":443") -> dict:
    """Create a new Caddy HTTP server block. server_name: identifier (e.g. 'srv0', 'my_server'). listen: comma-separated listen addresses (e.g. ':443', ':80,:443'). The new server starts with no routes — use add_reverse_proxy_route or other add_*_route tools to populate it."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "create_server"}
    server_name = server_name.strip()
    listen_addrs = [a.strip() for a in listen.split(",") if a.strip()]
    if not listen_addrs:
        return {"error": "listen must not be empty", "tool": "create_server"}
    for addr in listen_addrs:
        if not re.match(r'^(.*):(\d{1,5})$', addr):
            return {"error": f"Invalid listen address '{addr}': must be host:port or :port (e.g. ':443', '0.0.0.0:80')", "tool": "create_server"}
        port_str = addr.rsplit(":", 1)[-1]
        if not (1 <= int(port_str) <= 65535):
            return {"error": f"Invalid port {port_str} in listen address '{addr}': must be 1-65535", "tool": "create_server"}
    try:
        server_cfg = {"listen": listen_addrs, "routes": []}
        resp = await _request("PUT", f"/config/apps/http/servers/{server_name}", json=server_cfg)
        resp.raise_for_status()
        return {"result": {"created": True, "name": server_name, "listen": listen_addrs}}
    except Exception as e:
        err = _err(e, "create_server"); err["server_name"] = server_name; return err


def _validate_upstream_dial(upstream: str) -> str | None:
    """Return an error string if upstream is not a valid Caddy dial address, else None."""
    if upstream.startswith("unix/"):
        return None
    if ":" not in upstream:
        return f"upstream '{upstream}' must be in format host:port (e.g. 'localhost:8080') or unix//path/to/socket"
    port_str = upstream.rsplit(":", 1)[1]
    try:
        port = int(port_str)
        if not 1 <= port <= 65535:
            return f"upstream port {port} out of range 1-65535"
    except ValueError:
        return f"upstream '{upstream}' has non-numeric port '{port_str}'"
    return None


_GO_DURATION_RE = re.compile(r'^0$|^\d+(?:ns|us|ms|s|m|h)$')


def _validate_go_duration(value: str, field: str) -> str | None:
    """Return error string if value is not a valid Go duration (e.g. '30s', '1m', '500ms', '0'), else None."""
    if not _GO_DURATION_RE.match(value.strip()):
        return f"{field} must be a valid Go duration (e.g. '30s', '1m', '500ms', or '0' to disable), got '{value}'"
    return None


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
    """List all configured routes (virtual hosts) with hosts, handler, upstreams, listen addresses, and route index."""
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        routes_out = []

        for server_name, server in servers.items():
            listen = server.get("listen", [])
            for idx, route in enumerate(server.get("routes", [])):
                hosts = []
                paths = []
                for match in route.get("match", []):
                    hosts.extend(match.get("host", []))
                    paths.extend(match.get("path", []))

                handles = route.get("handle", [])
                handler_type = handles[0].get("handler") if handles else None
                upstreams = _extract_upstreams(handles)

                routes_out.append({
                    "server": server_name,
                    "index": idx,
                    "listen": listen,
                    "hosts": hosts,
                    "paths": paths,
                    "handler": handler_type,
                    "upstreams": upstreams,
                })

        return {"result": routes_out}
    except Exception as e:
        return _err(e, "list_routes")


@mcp.tool()
async def add_reverse_proxy_route(host: str, upstream: str, server_name: str = "", path_prefix: str = "", health_path: str = "", health_interval: str = "") -> dict:
    """Add a reverse proxy route to Caddy. host: domain (e.g. 'app.example.com'). upstream: backend dial address (e.g. 'localhost:3000'). path_prefix: optional URL path prefix to match in addition to host (e.g. '/api/*'). server_name: auto-detects first server if empty. health_path: optional active health check path (e.g. '/health'). health_interval: optional health check interval as Go duration (e.g. '10s', '1m') — only used if health_path is set."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_reverse_proxy_route"}
    host = host.strip()
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_reverse_proxy_route"}
    upstream = upstream.strip()
    if err := _validate_upstream_dial(upstream):
        return {"error": err, "tool": "add_reverse_proxy_route"}
    if health_interval and health_interval.strip() and (not health_path or not health_path.strip()):
        return {"error": "health_interval requires health_path to be set", "tool": "add_reverse_proxy_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")

        match_rule: dict = {"host": [host]}
        if path_prefix:
            p = path_prefix.strip().rstrip("/")
            if not p.startswith("/"):
                p = "/" + p
            if not p.endswith("*"):
                match_rule["path"] = [p, p + "/*"]
            else:
                match_rule["path"] = [p]
        handler: dict = {"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}
        if health_path and health_path.strip():
            handler["health_checks"] = {"active": {"uri": health_path.strip()}}
            if health_interval and health_interval.strip():
                if err := _validate_go_duration(health_interval.strip(), "health_interval"):
                    return {"error": err, "tool": "add_reverse_proxy_route"}
                handler["health_checks"]["active"]["interval"] = health_interval.strip()
        route = {
            "match": [match_rule],
            "handle": [handler],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "upstream": upstream, "path_prefix": path_prefix.strip() or None, "health_path": health_path.strip() or None, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_reverse_proxy_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_path_route(path: str, upstream: str, server_name: str = "") -> dict:
    """Add a path-based reverse proxy route. All requests matching the URL path prefix are proxied to upstream regardless of hostname. path: e.g. '/api/*' or '/v2/*'. upstream: backend address (e.g., 'localhost:8080'). server_name: auto-detects first server if empty."""
    if not path or not path.strip():
        return {"error": "path must not be empty", "tool": "add_path_route"}
    path = path.strip()
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_path_route"}
    upstream = upstream.strip()
    if err := _validate_upstream_dial(upstream):
        return {"error": err, "tool": "add_path_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")

        route = {
            "match": [{"path": [path]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"path": path, "upstream": upstream, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_path_route")
        err["path"] = path
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_static_file_server(path: str, root: str, server_name: str = "") -> dict:
    """Add a static file server route to Caddy. path: URL path to match (e.g., '/files/*'). root: filesystem directory to serve. server_name: auto-detects first server if empty."""
    if not path or not path.strip():
        return {"error": "path must not be empty", "tool": "add_static_file_server"}
    path = path.strip()
    if not root or not root.strip():
        return {"error": "root must not be empty", "tool": "add_static_file_server"}
    root = root.strip()
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")

        route = {
            "match": [{"path": [path]}],
            "handle": [{"handler": "file_server", "root": root}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "path": path, "root": root, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_static_file_server")
        err["path"] = path
        err["root"] = root
        return err


@mcp.tool()
async def add_redirect(from_host: str, to_url: str, status_code: int = 301, server_name: str = "") -> dict:
    """Add an HTTP redirect route. from_host: domain to redirect (e.g. 'old.example.com'). to_url: destination URL. status_code: 301 (permanent), 302 (temporary), 307, or 308."""
    if not from_host or not from_host.strip():
        return {"error": "from_host must not be empty", "tool": "add_redirect"}
    from_host = from_host.strip()
    if not to_url or not to_url.strip():
        return {"error": "to_url must not be empty", "tool": "add_redirect"}
    to_url = to_url.strip()
    if status_code not in {301, 302, 307, 308}:
        return {"error": "status_code must be 301, 302, 307, or 308", "tool": "add_redirect"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")

        route = {
            "match": [{"host": [from_host]}],
            "handle": [{
                "handler": "static_response",
                "status_code": status_code,
                "headers": {"Location": [to_url]},
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"from": from_host, "to": to_url, "status_code": status_code, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_redirect")
        err["from_host"] = from_host
        err["to_url"] = to_url
        return err


@mcp.tool()
async def add_header_route(host: str, header_name: str, header_value: str, server_name: str = "") -> dict:
    """Add a route that injects a response header for all requests to a given host. Useful for HSTS, CORS, X-Frame-Options, X-Content-Type-Options, etc. host: domain to match. server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_header_route"}
    host = host.strip()
    if not header_name or not header_name.strip():
        return {"error": "header_name must not be empty", "tool": "add_header_route"}
    header_name = header_name.strip()
    if not header_value or not header_value.strip():
        return {"error": "header_value must not be empty", "tool": "add_header_route"}
    header_value = header_value.strip()
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")

        route = {
            "match": [{"host": [host]}],
            "handle": [{
                "handler": "headers",
                "response": {"set": {header_name: [header_value]}},
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "header": header_name, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_header_route")
        err["host"] = host
        return err


@mcp.tool()
async def delete_route(server_name: str, route_index: int) -> dict:
    """Delete a route by index from an HTTP server's route list. Use list_routes to find the index. Changes take effect immediately."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "delete_route"}
    server_name = server_name.strip()
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "delete_route"}
    try:
        resp = await _request("DELETE", f"/config/apps/http/servers/{server_name}/routes/{route_index}")
        resp.raise_for_status()
        return {"result": {"server_name": server_name, "route_index": route_index, "deleted": True}}
    except Exception as e:
        err = _err(e, "delete_route"); err["server_name"] = server_name; err["route_index"] = route_index; return err


@mcp.tool()
async def list_upstreams() -> dict:
    """List all reverse proxy upstreams with their health status and request counts. Returns empty list if no upstreams are configured."""
    try:
        resp = await _request("GET", "/reverse_proxy/upstreams")
        resp.raise_for_status()
        return {"result": resp.json()}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"result": []}
        return _err(e, "list_upstreams")
    except Exception as e:
        return _err(e, "list_upstreams")


@mcp.tool()
async def mark_upstream_health(upstream_address: str, healthy: bool) -> dict:
    """Manually override the health state of a reverse proxy upstream. upstream_address: the dial address as shown by list_upstreams (e.g. '10.0.0.5:8080'). healthy: True to mark healthy, False to mark unhealthy. Persists until Caddy's active health checks re-evaluate or the server restarts."""
    if not upstream_address or not upstream_address.strip():
        return {"error": "upstream_address must not be empty", "tool": "mark_upstream_health"}
    upstream_address = upstream_address.strip()
    try:
        resp = await _request(
            "POST",
            "/reverse_proxy/upstreams/health",
            json={"address": upstream_address, "healthy": healthy},
        )
        resp.raise_for_status()
        return {"result": {"upstream": upstream_address, "healthy": healthy}}
    except Exception as e:
        err = _err(e, "mark_upstream_health"); err["upstream_address"] = upstream_address; return err


@mcp.tool()
async def get_certificates() -> dict:
    """List TLS automation policies from Caddy config: domains, ACME issuers, and CAs. Returns policies, not live certificate objects — use list_loaded_certs to see the actual certificate cache."""
    try:
        resp = await _request("GET", "/config/apps/tls/automation/policies")
        if resp.status_code == 404:
            return {"result": []}
        resp.raise_for_status()
        policies = resp.json() or []

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
    caddyfile = caddyfile.strip()
    try:
        resp = await _request(
            "POST",
            "/adapt",
            params={"adapter": "caddyfile"},
            content=caddyfile.encode(),
            headers={"Content-Type": "text/caddyfile"},
        )
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "adapt_config")


@mcp.tool()
async def reload(source: str = "") -> dict:
    """Reload Caddy config. source: raw JSON string (starts with '{'), path to a JSON config file, or empty to reload the current running configuration in-place (useful for applying Caddy version upgrades or resetting to the live state)."""
    try:
        if not source or not source.strip():
            # Fetch and re-POST current running config
            cur = await _request("GET", "/config/")
            cur.raise_for_status()
            config_data = cur.json()
            if config_data is None:
                return {"error": "No config currently loaded in Caddy. Provide a source config to load.", "tool": "reload"}
        elif source.lstrip().startswith("{"):
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
    if not config_path or not config_path.strip():
        return {"error": "config_path must not be empty", "tool": "update_config_path"}
    if not value or not value.strip():
        return {"error": "value must not be empty", "tool": "update_config_path"}
    config_path = config_path.strip()
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
        err = _err(e, "update_config_path"); err["config_path"] = config_path; return err


@mcp.tool()
async def delete_config_path(config_path: str) -> dict:
    """Delete a specific Caddy config node at the given path. config_path: e.g. '/apps/http/servers/srv0'. Changes take effect immediately."""
    if not config_path or not config_path.strip():
        return {"error": "config_path must not be empty", "tool": "delete_config_path"}
    config_path = config_path.strip()
    if not config_path.startswith("/"):
        return {"error": "config_path must start with '/'", "tool": "delete_config_path"}
    try:
        resp = await _request("DELETE", f"/config{config_path}")
        resp.raise_for_status()
        return {"result": {"deleted": True, "path": config_path}}
    except Exception as e:
        err = _err(e, "delete_config_path"); err["config_path"] = config_path; return err


@mcp.tool()
async def add_tls_policy(subjects: str, ca_url: str = "", email: str = "") -> dict:
    """Add a TLS automation policy for one or more domains. subjects: comma-separated domain names (e.g., 'example.com,*.example.com'). ca_url: ACME CA directory URL (defaults to Let's Encrypt if empty). email: ACME account email. Appends to existing TLS policies."""
    if not subjects or not subjects.strip():
        return {"error": "subjects must not be empty", "tool": "add_tls_policy"}
    subjects = subjects.strip()
    subject_list = [s.strip() for s in subjects.split(",") if s.strip()]
    if not subject_list:
        return {"error": "subjects must contain at least one domain", "tool": "add_tls_policy"}

    issuer: dict = {"module": "acme"}
    if ca_url and ca_url.strip():
        issuer["ca"] = ca_url.strip()
    if email and email.strip():
        issuer["email"] = email.strip()

    policy = {
        "subjects": subject_list,
        "issuers": [issuer],
    }

    try:
        pol_resp = await _request("GET", "/config/apps/tls/automation/policies")
        policies = (pol_resp.json() or []) if pol_resp.status_code == 200 else []
        policies.append(policy)

        # PATCH /config/apps/tls/automation — works if TLS app exists.
        # If TLS section absent, create the full structure at /config/apps/tls.
        try:
            patch_resp = await _request("PATCH", "/config/apps/tls/automation", json={"policies": policies})
            patch_resp.raise_for_status()
        except httpx.HTTPStatusError as patch_err:
            if patch_err.response.status_code in {400, 404}:
                tls_app = {"automation": {"policies": policies}}
                create_resp = await _request("PATCH", "/config/apps/tls", json=tls_app)
                create_resp.raise_for_status()
            else:
                raise
        return {"result": {"added": True, "subjects": subject_list, "issuer_module": issuer["module"]}}
    except Exception as e:
        err = _err(e, "add_tls_policy"); err["subjects"] = subject_list; return err


@mcp.tool()
async def list_tls_policies() -> dict:
    """List all TLS automation policies configured in Caddy, including subjects and ACME issuer details. Returns empty list if no policies configured."""
    try:
        resp = await _request("GET", "/config/apps/tls/automation/policies")
        resp.raise_for_status()
        return {"result": resp.json() or []}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"result": []}
        return _err(e, "list_tls_policies")
    except Exception as e:
        return _err(e, "list_tls_policies")


@mcp.tool()
async def update_tls_policy(policy_index: int, subjects: str = "", ca_url: str = "", email: str = "") -> dict:
    """Update an existing TLS automation policy by index. Fetches the current policy, applies non-empty changes, and PUTs the result. Use list_tls_policies to find indices. subjects: comma-separated domains to replace subjects list. ca_url: new ACME CA URL. email: new ACME account email."""
    if policy_index < 0:
        return {"error": "policy_index must be >= 0", "tool": "update_tls_policy"}
    if not subjects and not ca_url and not email:
        return {"error": "At least one of subjects, ca_url, or email must be specified", "tool": "update_tls_policy"}
    try:
        resp = await _request("GET", f"/config/apps/tls/automation/policies/{policy_index}")
        resp.raise_for_status()
        policy = resp.json() or {}
        if subjects and subjects.strip():
            policy["subjects"] = [s.strip() for s in subjects.split(",") if s.strip()]
        if ca_url and ca_url.strip():
            issuers = policy.get("issuers", [{}])
            if issuers:
                issuers[0]["ca"] = ca_url.strip()
            else:
                issuers = [{"module": "acme", "ca": ca_url.strip()}]
            policy["issuers"] = issuers
        if email and email.strip():
            issuers = policy.get("issuers", [{}])
            if issuers:
                issuers[0]["email"] = email.strip()
            else:
                issuers = [{"module": "acme", "email": email.strip()}]
            policy["issuers"] = issuers
        put_resp = await _request("PUT", f"/config/apps/tls/automation/policies/{policy_index}", json=policy)
        put_resp.raise_for_status()
        return {"result": {"updated": True, "index": policy_index}}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"TLS policy at index {policy_index} not found", "tool": "update_tls_policy"}
        err = _err(e, "update_tls_policy"); err["policy_index"] = policy_index; return err
    except Exception as e:
        err = _err(e, "update_tls_policy"); err["policy_index"] = policy_index; return err


@mcp.tool()
async def get_tls_policy(policy_index: int) -> dict:
    """Get a single TLS automation policy by its 0-based index. Returns subjects, issuers, and ACME settings. Use list_tls_policies to see all indices."""
    if policy_index < 0:
        return {"error": "policy_index must be >= 0", "tool": "get_tls_policy"}
    try:
        resp = await _request("GET", f"/config/apps/tls/automation/policies/{policy_index}")
        resp.raise_for_status()
        return {"result": {"index": policy_index, "policy": resp.json()}}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"TLS policy at index {policy_index} not found", "tool": "get_tls_policy"}
        err = _err(e, "get_tls_policy"); err["policy_index"] = policy_index; return err
    except Exception as e:
        err = _err(e, "get_tls_policy"); err["policy_index"] = policy_index; return err


@mcp.tool()
async def delete_tls_policy(policy_index: int) -> dict:
    """Delete a TLS automation policy by its 0-based index. Use list_tls_policies to see indices. Changes take effect immediately."""
    if policy_index < 0:
        return {"error": "policy_index must be >= 0", "tool": "delete_tls_policy"}
    try:
        resp = await _request("DELETE", f"/config/apps/tls/automation/policies/{policy_index}")
        resp.raise_for_status()
        return {"result": {"deleted": True, "index": policy_index}}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"TLS policy at index {policy_index} not found", "tool": "delete_tls_policy"}
        err = _err(e, "delete_tls_policy"); err["policy_index"] = policy_index; return err
    except Exception as e:
        err = _err(e, "delete_tls_policy"); err["policy_index"] = policy_index; return err


@mcp.tool()
async def update_route(server_name: str, route_index: int, config_json: str) -> dict:
    """Replace a route in-place by index. config_json: full route JSON object. Use get_route to retrieve the current config, modify it, then pass it here. Changes take effect immediately."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "update_route"}
    server_name = server_name.strip()
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "update_route"}
    if not config_json or not config_json.strip():
        return {"error": "config_json must not be empty", "tool": "update_route"}
    config_json = config_json.strip()
    try:
        route_config = json.loads(config_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "tool": "update_route"}
    try:
        resp = await _request(
            "PUT",
            f"/config/apps/http/servers/{server_name}/routes/{route_index}",
            json=route_config,
        )
        resp.raise_for_status()
        return {"result": {"updated": True, "server_name": server_name, "route_index": route_index}}
    except Exception as e:
        err = _err(e, "update_route"); err["server_name"] = server_name; err["route_index"] = route_index; return err


@mcp.tool()
async def add_https_redirect(host: str, http_server_name: str = "") -> dict:
    """Add a permanent HTTP-to-HTTPS redirect for a host. Creates a 301 route on the :80 listener that redirects all requests to https://. Auto-detects a server listening on :80, or creates one named 'http_redirect'. This is the standard Caddy pattern for forcing HTTPS. http_server_name: override auto-detected :80 server name."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_https_redirect"}
    host = host.strip()
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}

        if http_server_name:
            if http_server_name.strip() not in servers:
                return {"error": f"Server '{http_server_name}' not found. Use list_servers to see available servers.", "tool": "add_https_redirect"}
            target_server = http_server_name.strip()
        else:
            target_server = None
            for name, srv in servers.items():
                if any(addr in (":80", ":http", "0.0.0.0:80") for addr in srv.get("listen", [])):
                    target_server = name
                    break
            if not target_server:
                create = await _request("PUT", "/config/apps/http/servers/http_redirect", json={"listen": [":80"], "routes": []})
                create.raise_for_status()
                target_server = "http_redirect"

        route = {
            "match": [{"host": [host]}],
            "handle": [{
                "handler": "static_response",
                "status_code": 301,
                "headers": {"Location": ["https://{http.request.host}{http.request.uri}"]},
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{target_server}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{target_server}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "server": target_server, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_https_redirect"); err["host"] = host; return err


@mcp.tool()
async def delete_https_redirect(host: str, http_server_name: str = "") -> dict:
    """Remove the HTTP-to-HTTPS redirect route for a specific host from the :80 listener. Scans all routes on the server listening on :80 and removes any that match only this host with a 301 redirect handler. http_server_name: override auto-detected :80 server name."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "delete_https_redirect"}
    host = host.strip()
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        if http_server_name:
            target_server = http_server_name.strip()
        else:
            target_server = next(
                (name for name, srv in servers.items()
                 if any(addr in (":80", ":http", "0.0.0.0:80") for addr in srv.get("listen", []))),
                None,
            )
        if not target_server or target_server not in servers:
            return {"error": "No server found listening on :80. Use list_servers to find HTTP redirect servers.", "tool": "delete_https_redirect"}
        routes = servers[target_server].get("routes", [])
        new_routes = []
        removed = 0
        for route in routes:
            matchers = route.get("match", [{}])
            hosts_in_route = matchers[0].get("host", []) if matchers else []
            is_redirect = any(
                h.get("handler") == "static_response" and h.get("status_code") in (301, "301")
                for h in route.get("handle", [])
            )
            if hosts_in_route == [host] and is_redirect:
                removed += 1
            else:
                new_routes.append(route)
        if removed == 0:
            return {"error": f"No HTTPS redirect route found for host '{host}' on server '{target_server}'", "tool": "delete_https_redirect"}
        put_resp = await _request("PUT", f"/config/apps/http/servers/{target_server}/routes", json=new_routes)
        put_resp.raise_for_status()
        return {"result": {"deleted": True, "host": host, "server": target_server, "routes_removed": removed}}
    except Exception as e:
        err = _err(e, "delete_https_redirect"); err["host"] = host; return err


@mcp.tool()
async def list_pki_cas() -> dict:
    """List Caddy-managed PKI certificate authorities (local CAs used for internal TLS / mTLS). Returns CA names and certificate info."""
    try:
        resp = await _request("GET", "/pki/ca")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_pki_cas")


@mcp.tool()
async def update_listen_addresses(server_name: str, listen_addresses: str) -> dict:
    """Update the listen addresses for an existing Caddy HTTP server block without touching routes. listen_addresses: comma-separated bind addresses (e.g., ':80,:443'). Changes take effect immediately."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "update_listen_addresses"}
    server_name = server_name.strip()
    if not listen_addresses or not listen_addresses.strip():
        return {"error": "listen_addresses must not be empty", "tool": "update_listen_addresses"}
    listen_addresses = listen_addresses.strip()
    listen = [a.strip() for a in listen_addresses.split(",") if a.strip()]
    if not listen:
        return {"error": "listen_addresses produced no valid entries", "tool": "update_listen_addresses"}
    for addr in listen:
        if not re.match(r'^(.*):(\d{1,5})$', addr):
            return {"error": f"Invalid listen address '{addr}': must be host:port or :port (e.g. ':443', '0.0.0.0:80')", "tool": "update_listen_addresses"}
        port_str = addr.rsplit(":", 1)[-1]
        if not (1 <= int(port_str) <= 65535):
            return {"error": f"Invalid port {port_str} in '{addr}': must be 1-65535", "tool": "update_listen_addresses"}
    try:
        resp = await _request(
            "PATCH",
            f"/config/apps/http/servers/{server_name}/listen",
            json=listen,
        )
        resp.raise_for_status()
        return {"result": {"updated": True, "name": server_name, "listen": listen}}
    except Exception as e:
        err = _err(e, "update_listen_addresses"); err["server_name"] = server_name; return err


@mcp.tool()
async def get_pki_ca(ca_name: str) -> dict:
    """Get details for a Caddy PKI certificate authority: root cert, intermediate cert PEM, signing policy, and lifetime. Use list_pki_cas to find CA names. Default local CA is named 'local'."""
    if not ca_name or not ca_name.strip():
        return {"error": "ca_name must not be empty", "tool": "get_pki_ca"}
    ca_name = ca_name.strip()
    try:
        resp = await _request("GET", f"/pki/ca/{ca_name}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_pki_ca"); err["ca_name"] = ca_name; return err



@mcp.tool()
async def update_upstream(server_name: str, route_index: int, new_upstream: str) -> dict:
    """Update the backend dial address for a reverse proxy route. Fetches the route, replaces all upstream dial addresses, and PATCHes in-place. Use list_routes to find server_name and route_index. new_upstream: e.g. 'localhost:8080' or '10.0.0.5:3000'."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "update_upstream"}
    server_name = server_name.strip()
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "update_upstream"}
    if not new_upstream or not new_upstream.strip():
        return {"error": "new_upstream must not be empty", "tool": "update_upstream"}
    new_upstream = new_upstream.strip()
    if err := _validate_upstream_dial(new_upstream):
        return {"error": err, "tool": "update_upstream"}
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes/{route_index}")
        resp.raise_for_status()
        route = resp.json()

        def _patch_upstreams(handles: list, dial: str) -> bool:
            found = False
            for h in handles:
                if h.get("handler") == "reverse_proxy":
                    h["upstreams"] = [{"dial": dial}]
                    found = True
                elif h.get("handler") == "subroute":
                    for sub in h.get("routes", []):
                        if _patch_upstreams(sub.get("handle", []), dial):
                            found = True
            return found

        modified = _patch_upstreams(route.get("handle", []), new_upstream)
        if not modified:
            return {"error": f"Route {route_index} has no reverse_proxy handler; use update_route for other handler types", "tool": "update_upstream"}

        patch_resp = await _request(
            "PUT",
            f"/config/apps/http/servers/{server_name}/routes/{route_index}",
            json=route,
        )
        patch_resp.raise_for_status()
        return {"result": {"updated": True, "server": server_name, "route_index": route_index, "upstream": new_upstream}}
    except Exception as e:
        err = _err(e, "update_upstream"); err["server_name"] = server_name; err["route_index"] = route_index; err["new_upstream"] = new_upstream; return err



@mcp.tool()
async def get_log_config() -> dict:
    """Fetch the current Caddy logging configuration. Returns configured log writers, encoders, sampling settings, and which loggers are enabled. Returns an empty object if logging is not explicitly configured (Caddy uses stderr by default)."""
    try:
        resp = await _request("GET", "/config/logging")
        resp.raise_for_status()
        return {"result": resp.json() or {}}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"result": {}, "note": "No logging config — Caddy uses stderr by default"}
        return _err(e, "get_log_config")
    except Exception as e:
        return _err(e, "get_log_config")


@mcp.tool()
async def update_log_config(config_json: str) -> dict:
    """Replace the Caddy logging configuration. config_json: full logging config object. Example: {"logs": {"default": {"writer": {"output": "file", "filename": "/var/log/caddy/access.log"}, "encoder": {"format": "json"}}}}. Use get_log_config to fetch the current config first."""
    if not config_json or not config_json.strip():
        return {"error": "config_json must not be empty", "tool": "update_log_config"}
    config_json = config_json.strip()
    try:
        log_cfg = json.loads(config_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "tool": "update_log_config"}
    try:
        resp = await _request("PUT", "/config/logging", json=log_cfg)
        resp.raise_for_status()
        return {"result": {"updated": True}}
    except Exception as e:
        return _err(e, "update_log_config")


@mcp.tool()
async def add_basicauth_route(host: str, username: str, hashed_password: str, upstream: str = "", server_name: str = "") -> dict:
    """Add a route protected by HTTP Basic Authentication. hashed_password: bcrypt hash — generate with: caddy hash-password --plaintext 'yourpassword'. upstream: backend to proxy authenticated requests to (e.g. 'localhost:8080'); if empty, returns 200 OK. WARNING: Basic auth sends credentials on every request — only use over HTTPS."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_basicauth_route"}
    if not username or not username.strip():
        return {"error": "username must not be empty", "tool": "add_basicauth_route"}
    if not hashed_password or not hashed_password.strip():
        return {"error": "hashed_password must not be empty — provide a bcrypt hash", "tool": "add_basicauth_route"}
    hashed_password = hashed_password.strip()
    if not re.match(r'^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$', hashed_password):
        return {"error": "hashed_password must be a valid bcrypt hash (e.g. from: caddy hash-password --plaintext 'pw')", "tool": "add_basicauth_route"}
    if upstream and upstream.strip():
        if err := _validate_upstream_dial(upstream.strip()):
            return {"error": err, "tool": "add_basicauth_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        proxy_handler = (
            {"handler": "reverse_proxy", "upstreams": [{"dial": upstream.strip()}]}
            if upstream and upstream.strip()
            else {"handler": "static_response", "status_code": 200, "body": "Authenticated"}
        )
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [
                {
                    "handler": "authentication",
                    "providers": {
                        "http_basic": {
                            "hash": {"algorithm": "bcrypt"},
                            "accounts": [{"username": username.strip(), "password": hashed_password.strip()}],
                        }
                    },
                },
                proxy_handler,
            ],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "username": username, "upstream": upstream or "(none)", "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_basicauth_route")
        err["host"] = host
        if upstream:
            err["upstream"] = upstream
        return err


@mcp.tool()
async def stop_caddy() -> dict:
    """Gracefully stop the Caddy server process. WARNING: this terminates the Caddy admin API — the server will be unreachable until restarted externally."""
    try:
        resp = await _request("POST", "/stop")
        resp.raise_for_status()
        return {"result": {"stopped": True}}
    except Exception as e:
        return _err(e, "stop_caddy")


@mcp.tool()
async def list_loaded_certs() -> dict:
    """List all TLS certificates currently loaded in Caddy's live certificate cache. Returns subject, issuer, expiry, and SANs for each certificate. Different from get_certificates which shows automation policies."""
    try:
        resp = await _request("GET", "/certificates")
        resp.raise_for_status()
        return {"result": resp.json()}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"result": [], "note": "No certificates loaded in cache"}
        return _err(e, "list_loaded_certs")
    except Exception as e:
        return _err(e, "list_loaded_certs")


@mcp.tool()
async def add_rewrite_route(host: str, path_prefix: str, upstream: str, server_name: str = "") -> dict:
    """Add a reverse proxy route that strips a path prefix before forwarding to upstream. Requests to host/path_prefix/foo are forwarded as /foo to upstream. host: domain to match. path_prefix: URL prefix to strip and match (e.g. '/api/v1'). upstream: backend address. server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_rewrite_route"}
    host = host.strip()
    if not path_prefix or not path_prefix.strip():
        return {"error": "path_prefix must not be empty", "tool": "add_rewrite_route"}
    path_prefix = path_prefix.strip()
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_rewrite_route"}
    upstream = upstream.strip()
    if err := _validate_upstream_dial(upstream):
        return {"error": err, "tool": "add_rewrite_route"}
    prefix = path_prefix.strip().rstrip("/")
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host], "path": [prefix, prefix + "/*"]}],
            "handle": [
                {"handler": "rewrite", "strip_path_prefix": prefix},
                {"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]},
            ],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "path_prefix": prefix, "upstream": upstream, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_rewrite_route"); err["host"] = host; err["upstream"] = upstream; return err


@mcp.tool()
async def add_compress_route(host: str, server_name: str = "", algorithms: str = "zstd,gzip") -> dict:
    """Add a compression (encode) route for a host to enable gzip/zstd response compression. Caddy automatically negotiates the best algorithm based on Accept-Encoding. algorithms: comma-separated list of encoders — 'zstd' (preferred), 'gzip', or both (default). server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_compress_route"}
    host = host.strip()
    algo_list = [a.strip().lower() for a in algorithms.split(",") if a.strip()]
    valid_algos = {"gzip", "zstd"}
    invalid = [a for a in algo_list if a not in valid_algos]
    if invalid:
        return {"error": f"Invalid algorithms: {invalid}. Use 'gzip' and/or 'zstd'", "tool": "add_compress_route"}
    if not algo_list:
        return {"error": "algorithms must not be empty", "tool": "add_compress_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        encodings = {a: {} for a in algo_list}
        route = {
            "match": [{"host": [host]}],
            "handle": [{"handler": "encode", "encodings": encodings, "prefer": algo_list}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "algorithms": algo_list, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_compress_route")
        err["host"] = host
        return err


@mcp.tool()
async def add_request_header_route(host: str, header_name: str, header_value: str, server_name: str = "") -> dict:
    """Add a route that injects a request header for all requests to a given host. The headers handler is middleware — it sets the header and passes the request to subsequent matching routes. Add a reverse proxy route separately (or use add_reverse_proxy_route which combines both). Useful for adding X-API-Key, Authorization, or any other header a backend requires. host: domain to match. server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_request_header_route"}
    host = host.strip()
    if not header_name or not header_name.strip():
        return {"error": "header_name must not be empty", "tool": "add_request_header_route"}
    header_name = header_name.strip()
    if not header_value or not header_value.strip():
        return {"error": "header_value must not be empty", "tool": "add_request_header_route"}
    header_value = header_value.strip()
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host]}],
            "handle": [{
                "handler": "headers",
                "request": {"set": {header_name: [header_value]}},
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "header": header_name, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_request_header_route"); err["host"] = host; return err


@mcp.tool()
async def add_cors_route(
    host: str,
    upstream: str = "",
    allow_origins: str = "*",
    allow_methods: str = "GET,POST,PUT,DELETE,OPTIONS",
    allow_headers: str = "Content-Type,Authorization",
    max_age: int = 3600,
    allow_credentials: bool = False,
    server_name: str = "",
) -> dict:
    """Add a CORS route for a host. If upstream is provided, creates a complete CORS proxy route: OPTIONS preflight returns 200 with CORS headers, all other methods are proxied to upstream with CORS headers on the response. If upstream is omitted, adds a headers-only overlay (useful for static file servers already configured separately). host: domain to match. upstream: backend address (e.g. 'localhost:3000'). allow_origins: comma-separated origins or '*'. allow_methods: comma-separated HTTP methods. allow_headers: comma-separated header names. max_age: preflight cache seconds. allow_credentials: send Access-Control-Allow-Credentials: true (requires a specific origin, cannot be used with allow_origins='*')."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_cors_route"}
    host = host.strip()
    origins = allow_origins.strip() or "*"
    # CORS spec: Access-Control-Allow-Origin must be *, a single origin, or "null"
    # Multiple comma-separated origins in one header is not valid per RFC 6454
    if origins != "*" and "," in origins:
        return {
            "error": "allow_origins must be '*' or a single origin URL. The CORS spec does not allow multiple origins in one Access-Control-Allow-Origin header. Call add_cors_route once per origin, or use '*'.",
            "tool": "add_cors_route",
        }
    if allow_credentials and origins == "*":
        return {
            "error": "allow_credentials=True requires a specific origin — '*' is not permitted by the CORS spec when credentials are included. Set allow_origins to the exact origin (e.g. 'https://app.example.com').",
            "tool": "add_cors_route",
        }
    methods = allow_methods.strip() or "GET,POST,PUT,DELETE,OPTIONS"
    _valid_http_methods = {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
    method_list = [m.strip().upper() for m in methods.split(",") if m.strip()]
    invalid_methods = [m for m in method_list if m not in _valid_http_methods]
    if invalid_methods:
        return {"error": f"Invalid HTTP methods in allow_methods: {invalid_methods}. Valid: {', '.join(sorted(_valid_http_methods))}", "tool": "add_cors_route"}
    hdrs = allow_headers.strip() or "Content-Type,Authorization"
    max_age = max(0, max_age)
    cors_headers = {
        "Access-Control-Allow-Origin": [origins],
        "Access-Control-Allow-Methods": [methods],
        "Access-Control-Allow-Headers": [hdrs],
        "Access-Control-Max-Age": [str(max_age)],
    }
    if allow_credentials:
        cors_headers["Access-Control-Allow-Credentials"] = ["true"]
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")

        if upstream and upstream.strip():
            upstream = upstream.strip()
            err = _validate_upstream_dial(upstream)
            if err:
                return {"error": err, "tool": "add_cors_route"}
            # Full CORS proxy: OPTIONS preflight + proxied requests with CORS headers
            route = {
                "match": [{"host": [host]}],
                "handle": [{
                    "handler": "subroute",
                    "routes": [
                        {
                            "match": [{"method": ["OPTIONS"]}],
                            "handle": [
                                {"handler": "headers", "response": {"set": cors_headers}},
                                {"handler": "static_response", "status_code": 200},
                            ],
                        },
                        {
                            "handle": [
                                {"handler": "headers", "response": {"set": cors_headers}},
                                {"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]},
                            ],
                        },
                    ],
                }],
            }
        else:
            # Headers-only overlay (for use with separately configured proxy/static routes)
            route = {
                "match": [{"host": [host]}],
                "handle": [{"handler": "headers", "response": {"set": cors_headers}}],
            }

        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {
            "result": {
                "host": host,
                "upstream": upstream.strip() if upstream else None,
                "allow_origins": origins,
                "allow_methods": methods,
                "allow_headers": hdrs,
                "max_age": max_age,
                "allow_credentials": allow_credentials,
                "server": server_name,
                "route_index": len(routes) - 1,
            }
        }
    except Exception as e:
        err = _err(e, "add_cors_route")
        err["host"] = host
        if upstream and upstream.strip():
            err["upstream"] = upstream.strip()
        return err


@mcp.tool()
async def delete_route_by_host(host: str, server_name: str = "") -> dict:
    """Delete all routes matching a specific hostname from the Caddy config. Useful when you know the host but not the route index. Use list_routes first to preview. Returns the count of deleted routes. server_name: auto-detects all servers if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "delete_route_by_host"}
    host = host.strip()
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        if not servers:
            return {"error": "No HTTP servers configured in Caddy", "tool": "delete_route_by_host"}
        if server_name:
            if server_name not in servers:
                return {"error": f"Server '{server_name}' not found", "tool": "delete_route_by_host"}
            target_servers = {server_name: servers[server_name]}
        else:
            target_servers = servers
        total_deleted = 0
        for srv_name, server in target_servers.items():
            routes = server.get("routes", [])
            kept = []
            srv_deleted = 0
            for route in routes:
                route_hosts = []
                for match in route.get("match", []):
                    route_hosts.extend(match.get("host", []))
                if host in route_hosts:
                    srv_deleted += 1
                else:
                    kept.append(route)
            if srv_deleted > 0:
                upd = await _request("PUT", f"/config/apps/http/servers/{srv_name}/routes", json=kept)
                upd.raise_for_status()
                total_deleted += srv_deleted
        return {"result": {"host": host, "deleted": total_deleted}}
    except Exception as e:
        err = _err(e, "delete_route_by_host"); err["host"] = host; return err


@mcp.tool()
async def add_ip_filter_route(host: str, upstream: str, allowed_ips: str, server_name: str = "") -> dict:
    """Add a reverse proxy route that only allows requests from specific IP addresses or CIDR ranges. Requests from other IPs receive a 403. host: domain to match. upstream: backend address. allowed_ips: comma-separated IPs or CIDRs (e.g., '192.168.1.0/24,10.0.0.5'). server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_ip_filter_route"}
    host = host.strip()
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_ip_filter_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_ip_filter_route"}
    if not allowed_ips or not allowed_ips.strip():
        return {"error": "allowed_ips must not be empty", "tool": "add_ip_filter_route"}
    allowed_ips = allowed_ips.strip()
    ip_list = [ip.strip() for ip in allowed_ips.split(",") if ip.strip()]
    if not ip_list:
        return {"error": "allowed_ips must contain at least one IP or CIDR", "tool": "add_ip_filter_route"}
    try:
        for ip in ip_list:
            ipaddress.ip_network(ip, strict=False)
    except ValueError as e:
        return {"error": f"Invalid IP/CIDR in allowed_ips: {e}", "tool": "add_ip_filter_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host], "remote_ip": {"ranges": ip_list}}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        deny_route = {
            "match": [{"host": [host]}],
            "handle": [{"handler": "static_response", "status_code": 403}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        routes.append(deny_route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {
            "result": {
                "host": host,
                "upstream": upstream,
                "allowed_ips": ip_list,
                "server": server_name,
                "allow_route_index": len(routes) - 2,
                "deny_route_index": len(routes) - 1,
            }
        }
    except Exception as e:
        err = _err(e, "add_ip_filter_route"); err["host"] = host; err["upstream"] = upstream; return err


@mcp.tool()
async def get_routes_by_host(host: str, server_name: str = "") -> dict:
    """Get all routes matching a specific hostname without deleting them. Returns route objects with their indices so you can inspect, update, or delete specific routes. server_name: auto-searches all servers if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "get_routes_by_host"}
    host = host.strip()
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        if not servers:
            return {"error": "No HTTP servers configured in Caddy", "tool": "get_routes_by_host"}
        if server_name:
            if server_name not in servers:
                return {"error": f"Server '{server_name}' not found", "tool": "get_routes_by_host"}
            target_servers = {server_name: servers[server_name]}
        else:
            target_servers = servers
        matches = []
        for srv_name, server in target_servers.items():
            for idx, route in enumerate(server.get("routes", [])):
                route_hosts = []
                for match in route.get("match", []):
                    route_hosts.extend(match.get("host", []))
                if host in route_hosts:
                    matches.append({"server_name": srv_name, "route_index": idx, "route": route})
        return {"result": {"host": host, "matches": matches, "count": len(matches)}}
    except Exception as e:
        err = _err(e, "get_routes_by_host"); err["host"] = host; return err


@mcp.tool()
async def get_route(server_name: str, route_index: int) -> dict:
    """Get a single route by server name and index. Use list_routes to find indices. Useful for inspecting a route before updating or deleting it."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "get_route"}
    server_name = server_name.strip()
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "get_route"}
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes/{route_index}")
        resp.raise_for_status()
        return {"result": {"server_name": server_name, "route_index": route_index, "route": resp.json()}}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"error": f"Route index {route_index} not found in server '{server_name}'", "tool": "get_route"}
        err = _err(e, "get_route"); err["server_name"] = server_name; err["route_index"] = route_index; return err
    except Exception as e:
        err = _err(e, "get_route"); err["server_name"] = server_name; err["route_index"] = route_index; return err


@mcp.tool()
async def add_maintenance_route(host: str, message: str = "Service temporarily unavailable. Please try again later.", server_name: str = "") -> dict:
    """Add a maintenance mode route for a host — all requests return 503 Service Unavailable with a plain-text message. Useful for taking a site offline during deployments. To end maintenance, use delete_route_by_host or delete_route with the returned route_index. server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_maintenance_route"}
    host = host.strip()
    message = message.strip() or "Service temporarily unavailable. Please try again later."
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host]}],
            "handle": [{
                "handler": "static_response",
                "status_code": 503,
                "body": message,
                "headers": {
                    "Content-Type": ["text/plain; charset=utf-8"],
                    "Retry-After": ["3600"],
                },
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "status_code": 503, "message": message, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_maintenance_route"); err["host"] = host; return err


@mcp.tool()
async def add_load_balanced_route(host: str, upstreams: str, lb_policy: str = "round_robin", cookie_name: str = "lb_cookie", server_name: str = "") -> dict:
    """Add a reverse proxy route with load balancing across multiple backends. host: domain to match. upstreams: comma-separated backend addresses (e.g. 'host1:8080,host2:8080,host3:8080'). lb_policy: round_robin (default), least_conn, ip_hash, first, random, random_choose, cookie. cookie_name: cookie name used when lb_policy='cookie' (default 'lb_cookie'). server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_load_balanced_route"}
    host = host.strip()
    if not upstreams or not upstreams.strip():
        return {"error": "upstreams must not be empty", "tool": "add_load_balanced_route"}
    upstream_list = [u.strip() for u in upstreams.split(",") if u.strip()]
    if len(upstream_list) < 2:
        return {"error": "add_load_balanced_route requires at least 2 upstreams. Use add_reverse_proxy_route for a single backend.", "tool": "add_load_balanced_route"}
    for u in upstream_list:
        if err := _validate_upstream_dial(u):
            return {"error": err, "tool": "add_load_balanced_route"}
    _VALID_POLICIES = {"round_robin", "least_conn", "ip_hash", "first", "random", "random_choose", "cookie"}
    lb_policy = lb_policy.strip().lower()
    if lb_policy not in _VALID_POLICIES:
        return {"error": f"Invalid lb_policy '{lb_policy}'. Valid: {', '.join(sorted(_VALID_POLICIES))}", "tool": "add_load_balanced_route"}
    selection_policy: dict = {"policy": lb_policy}
    if lb_policy == "cookie":
        selection_policy["name"] = cookie_name.strip() or "lb_cookie"
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host]}],
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": u} for u in upstream_list],
                "load_balancing": {"selection_policy": selection_policy},
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "upstreams": upstream_list, "lb_policy": lb_policy, "cookie_name": cookie_name if lb_policy == "cookie" else None, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_load_balanced_route")
        err["host"] = host
        err["upstreams"] = upstream_list
        return err


@mcp.tool()
async def list_modules() -> dict:
    """List all Caddy modules currently loaded in the running server. Useful for checking whether optional modules (rate_limit, crowdsec, etc.) are available before trying to use them in routes."""
    try:
        resp = await _request("GET", "/modules")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "list_modules")


@mcp.tool()
async def get_server_timeouts(server_name: str) -> dict:
    """Get the current request/response timeout settings for a Caddy HTTP server block: read_timeout, read_header_timeout, write_timeout, and idle_timeout. Returns only the fields that are explicitly configured — absent fields use Caddy's default (no timeout). Use set_server_timeouts to change them."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "get_server_timeouts"}
    server_name = server_name.strip()
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}")
        resp.raise_for_status()
        server_cfg = resp.json() or {}
        timeout_keys = ["read_timeout", "read_header_timeout", "write_timeout", "idle_timeout"]
        configured = {k: server_cfg[k] for k in timeout_keys if k in server_cfg}
        return {"result": {"server": server_name, "timeouts": configured, "note": "absent fields = no timeout (Caddy default)"}}
    except Exception as e:
        err = _err(e, "get_server_timeouts"); err["server_name"] = server_name; return err


@mcp.tool()
async def add_websocket_route(host: str, upstream: str, server_name: str = "", path_prefix: str = "") -> dict:
    """Add a reverse proxy route optimized for WebSocket and Server-Sent Events connections. Sets flush_interval=-1 to disable response buffering, which is required for WebSocket upgrades and streaming responses to work correctly. Caddy automatically handles the Connection: Upgrade and Upgrade: websocket headers. host: domain to match. upstream: backend WebSocket server (e.g. 'localhost:8080'). path_prefix: optional URL path to match (e.g. '/ws' or '/socket.io/*'). server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_websocket_route"}
    host = host.strip()
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_websocket_route"}
    upstream = upstream.strip()
    if err := _validate_upstream_dial(upstream):
        return {"error": err, "tool": "add_websocket_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        match_rule: dict = {"host": [host]}
        if path_prefix and path_prefix.strip():
            p = path_prefix.strip().rstrip("/")
            if not p.startswith("/"):
                p = "/" + p
            match_rule["path"] = [p, p + "/*"] if not p.endswith("*") else [p]
        route = {
            "match": [match_rule],
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream}],
                "flush_interval": -1,
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "upstream": upstream, "path_prefix": path_prefix.strip() or None, "flush_interval": -1, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_websocket_route"); err["host"] = host; err["upstream"] = upstream; return err


@mcp.tool()
async def add_php_fastcgi_route(host: str, php_fpm_address: str = "127.0.0.1:9000", root: str = "", server_name: str = "") -> dict:
    """Add a PHP FastCGI route for serving PHP applications via PHP-FPM. host: domain to match. php_fpm_address: PHP-FPM socket or address — TCP (e.g. '127.0.0.1:9000') or Unix socket (e.g. 'unix//run/php/php8.2-fpm.sock'). root: filesystem root for PHP files (e.g. '/var/www/html'). server_name: auto-detects first server if empty. Adds try_files (path → path/ → /index.php) for CMS clean URL support, then proxies *.php via FastCGI and falls back to static file_server for other assets."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_php_fastcgi_route"}
    host = host.strip()
    if not php_fpm_address or not php_fpm_address.strip():
        return {"error": "php_fpm_address must not be empty", "tool": "add_php_fastcgi_route"}
    php_fpm_address = php_fpm_address.strip()
    if err := _validate_upstream_dial(php_fpm_address):
        return {"error": err, "tool": "add_php_fastcgi_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        handle: list = []
        if root and root.strip():
            handle.append({"handler": "vars", "root": root.strip()})
        handle.extend([
            {
                "handler": "try_files",
                "files": ["{http.request.uri.path}", "{http.request.uri.path}/", "/index.php"],
            },
            {
                "handler": "subroute",
                "routes": [
                    {
                        "match": [{"path": ["*.php"]}],
                        "handle": [{
                            "handler": "reverse_proxy",
                            "transport": {
                                "protocol": "fastcgi",
                                "root": root.strip() if root and root.strip() else "",
                            },
                            "upstreams": [{"dial": php_fpm_address}],
                        }],
                    },
                    {
                        "handle": [{"handler": "file_server"}],
                    },
                ],
            },
        ])
        route = {
            "match": [{"host": [host]}],
            "handle": handle,
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "php_fpm": php_fpm_address, "root": root.strip() or None, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_php_fastcgi_route"); err["host"] = host; return err


@mcp.tool()
async def set_server_timeouts(server_name: str, read_timeout: str = "", read_header_timeout: str = "", write_timeout: str = "", idle_timeout: str = "") -> dict:
    """Configure request/response timeouts on a Caddy HTTP server block. All values are Go duration strings (e.g. '30s', '5m', '0' to disable). read_timeout: max time to read request headers and body. read_header_timeout: max time to read only request headers. write_timeout: max time to write response. idle_timeout: max time an idle keep-alive connection is kept open before closing. Use get_server to inspect current values. server_name: use list_servers to find names."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "set_server_timeouts"}
    server_name = server_name.strip()
    if not any([read_timeout, read_header_timeout, write_timeout, idle_timeout]):
        return {"error": "At least one timeout parameter must be specified", "tool": "set_server_timeouts"}
    timeouts: dict = {}
    for val, key in [
        (read_timeout, "read_timeout"), (read_header_timeout, "read_header_timeout"),
        (write_timeout, "write_timeout"), (idle_timeout, "idle_timeout"),
    ]:
        if val and val.strip():
            if err := _validate_go_duration(val.strip(), key):
                return {"error": err, "tool": "set_server_timeouts"}
            timeouts[key] = val.strip()
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}")
        resp.raise_for_status()
        server_cfg = resp.json() or {}
        server_cfg.update(timeouts)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}", json=server_cfg)
        put_resp.raise_for_status()
        return {"result": {"updated": True, "server": server_name, "timeouts": timeouts}}
    except Exception as e:
        err = _err(e, "set_server_timeouts"); err["server_name"] = server_name; return err


@mcp.tool()
async def add_try_files_route(host: str, root: str, fallback: str = "/index.html", server_name: str = "") -> dict:
    """Add a static file server route with try_files fallback — the standard pattern for SPAs (React, Vue, Angular, Svelte). Serves files from root on disk; if a file or directory is not found, rewrites to fallback (default /index.html). host: domain to match. root: filesystem path for static files (e.g. '/var/www/app/dist'). fallback: path to fall back to when file not found (default /index.html). server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_try_files_route"}
    host = host.strip()
    if not root or not root.strip():
        return {"error": "root must not be empty", "tool": "add_try_files_route"}
    root = root.strip()
    fallback = (fallback or "/index.html").strip()
    if not fallback.startswith("/"):
        fallback = "/" + fallback
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        server_name = server_name.strip()
        route = {
            "match": [{"host": [host]}],
            "handle": [{
                "handler": "subroute",
                "routes": [
                    {
                        "match": [{"not": [{"file": {"root": root, "try_files": ["{http.request.uri.path}", "{http.request.uri.path}/"]}}]}],
                        "handle": [{"handler": "rewrite", "uri": fallback}],
                    },
                    {
                        "handle": [{"handler": "file_server", "root": root}],
                    },
                ],
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "root": root, "fallback": fallback, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_try_files_route"); err["host"] = host; err["root"] = root; return err


@mcp.tool()
async def get_pki_ca_certificates(ca_name: str = "local") -> dict:
    """Get the certificate chain (root and intermediate PEM certificates) for a Caddy PKI certificate authority. Returns PEM-encoded root and intermediate certificates, useful for trust anchor configuration and certificate pinning. ca_name: CA name (default 'local' for the built-in local CA). Use list_pki_cas to find available CAs."""
    ca_name = (ca_name or "local").strip()
    if not ca_name:
        return {"error": "ca_name must not be empty", "tool": "get_pki_ca_certificates"}
    try:
        resp = await _request("GET", f"/pki/ca/{ca_name}/certificates")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        err = _err(e, "get_pki_ca_certificates"); err["ca_name"] = ca_name; return err


@mcp.tool()
async def renew_pki_ca(ca_name: str = "local") -> dict:
    """Force immediate renewal of a Caddy PKI certificate authority's root and intermediate certificates. Use this when the CA certificate is about to expire or has been revoked. ca_name: CA name (default 'local'). Use list_pki_cas to find available CAs."""
    ca_name = (ca_name or "local").strip()
    if not ca_name:
        return {"error": "ca_name must not be empty", "tool": "renew_pki_ca"}
    try:
        resp = await _request("POST", f"/pki/ca/{ca_name}/renew")
        resp.raise_for_status()
        return {"result": {"ca_name": ca_name, "renewed": True}}
    except Exception as e:
        err = _err(e, "renew_pki_ca"); err["ca_name"] = ca_name; return err


@mcp.tool()
async def add_stub_response_route(host: str, body: str = "", status_code: int = 200, content_type: str = "text/plain; charset=utf-8", path_prefix: str = "", server_name: str = "") -> dict:
    """Add a route that returns a fixed HTTP response without any backend. Useful for health check endpoints (e.g. /health → 200 OK), mock APIs, maintenance notices, or stub paths. host: domain to match. body: response body text (empty = no body). status_code: HTTP status code (default 200). content_type: default 'text/plain; charset=utf-8'. path_prefix: optional URL path to restrict the match. server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_stub_response_route"}
    host = host.strip()
    if not (100 <= status_code <= 599):
        return {"error": f"status_code must be 100-599, got {status_code}", "tool": "add_stub_response_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        server_name = server_name.strip()
        match_rule: dict = {"host": [host]}
        if path_prefix and path_prefix.strip():
            p = path_prefix.strip().rstrip("/")
            if not p.startswith("/"):
                p = "/" + p
            match_rule["path"] = [p, p + "/*"] if not p.endswith("*") else [p]
        handler: dict = {"handler": "static_response", "status_code": status_code}
        if body:
            handler["body"] = body
        if content_type and content_type.strip():
            handler["headers"] = {"Content-Type": [content_type.strip()]}
        route = {"match": [match_rule], "handle": [handler]}
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"host": host, "status_code": status_code, "path_prefix": path_prefix.strip() or None, "server": server_name, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_stub_response_route")
        err["host"] = host
        err["status_code"] = status_code
        return err


@mcp.tool()
async def add_global_headers(headers: str, server_name: str = "") -> dict:
    """Add response headers to ALL requests on a Caddy HTTP server, with no host matcher. Applied before route-specific handlers. Ideal for security headers (Strict-Transport-Security, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, etc.) that should apply everywhere. headers: 'Name: Value' pairs separated by newlines or semicolons. server_name: auto-detects first server if empty."""
    if not headers or not headers.strip():
        return {"error": "headers must not be empty", "tool": "add_global_headers"}
    parsed: dict[str, list[str]] = {}
    for raw in re.split(r"[;\n]", headers):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            return {"error": f"Invalid header '{raw}': must be 'Name: Value'", "tool": "add_global_headers"}
        hname, _, hval = raw.partition(":")
        parsed[hname.strip()] = [hval.strip()]
    if not parsed:
        return {"error": "No valid headers parsed", "tool": "add_global_headers"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        server_name = server_name.strip()
        route = {"handle": [{"handler": "headers", "response": {"set": parsed}}]}
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"headers": parsed, "server": server_name, "scope": "global", "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_global_headers"); err["server_name"] = server_name; return err


@mcp.tool()
async def delete_global_headers(server_name: str = "") -> dict:
    """Delete all global (no host matcher) header-only routes added by add_global_headers. These routes cannot be removed by delete_route_by_host because they have no match block. Removes every route in the server that has no match key and uses only a 'headers' handler. Returns the count of deleted routes. server_name: auto-detects first server if empty."""
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        if not servers:
            return {"error": "No HTTP servers configured in Caddy", "tool": "delete_global_headers"}
        if server_name:
            server_name = server_name.strip()
            if server_name not in servers:
                return {"error": f"Server '{server_name}' not found", "tool": "delete_global_headers"}
            target_servers = {server_name: servers[server_name]}
        else:
            target_servers = servers
        total_deleted = 0
        detail = []
        for sname, sdata in target_servers.items():
            routes = sdata.get("routes", [])
            indices_to_delete = []
            for i, route in enumerate(routes):
                if "match" in route:
                    continue
                handlers = route.get("handle", [])
                if len(handlers) == 1 and handlers[0].get("handler") == "headers":
                    indices_to_delete.append(i)
            for idx in reversed(indices_to_delete):
                dr = await _request("DELETE", f"/config/apps/http/servers/{sname}/routes/{idx}")
                dr.raise_for_status()
                total_deleted += 1
            if indices_to_delete:
                detail.append({"server": sname, "deleted_count": len(indices_to_delete)})
        return {"result": {"deleted": total_deleted, "detail": detail}}
    except Exception as e:
        err = _err(e, "delete_global_headers")
        if server_name:
            err["server_name"] = server_name
        return err


@mcp.tool()
async def move_route(server_name: str, from_index: int, to_index: int) -> dict:
    """Move a route to a different position in a Caddy HTTP server's route list. Route order matters — Caddy evaluates routes in order and the first matching route wins. Use list_routes to find current indices. Changes take effect immediately. server_name: server block name from list_servers."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "move_route"}
    server_name = server_name.strip()
    if from_index < 0:
        return {"error": "from_index must be >= 0", "tool": "move_route"}
    if to_index < 0:
        return {"error": "to_index must be >= 0", "tool": "move_route"}
    if from_index == to_index:
        return {"result": {"moved": False, "note": "from_index and to_index are the same"}}
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        resp.raise_for_status()
        routes = resp.json() or []
        n = len(routes)
        if from_index >= n:
            return {"error": f"from_index {from_index} out of range (server has {n} routes)", "tool": "move_route"}
        if to_index >= n:
            return {"error": f"to_index {to_index} out of range (server has {n} routes)", "tool": "move_route"}
        route = routes.pop(from_index)
        routes.insert(to_index, route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"moved": True, "from_index": from_index, "to_index": to_index, "server": server_name}}
    except Exception as e:
        err = _err(e, "move_route"); err["server_name"] = server_name; err["from_index"] = from_index; err["to_index"] = to_index; return err


@mcp.tool()
async def add_trusted_proxies(ranges: str = "private_ranges", server_name: str = "") -> dict:
    """Configure trusted proxy IP ranges for a Caddy HTTP server. When Caddy sits behind a load balancer, CDN, or reverse proxy, this tells Caddy which upstream IPs can be trusted to provide accurate X-Forwarded-For headers. ranges: 'private_ranges' to trust all RFC1918 private networks (recommended for internal deployments), or comma-separated CIDR blocks (e.g. '10.0.0.0/8,172.16.0.0/12,192.168.0.0/16'). server_name: auto-detects first server if empty."""
    ranges = (ranges or "private_ranges").strip()
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        server_name = server_name.strip()
        if ranges == "private_ranges":
            trusted: dict = {"source": "private_ranges"}
        else:
            cidr_list = [r.strip() for r in ranges.split(",") if r.strip()]
            if not cidr_list:
                return {"error": "ranges must contain at least one CIDR or 'private_ranges'", "tool": "add_trusted_proxies"}
            import ipaddress as _ipa
            for cidr in cidr_list:
                try:
                    _ipa.ip_network(cidr, strict=False)
                except ValueError as ve:
                    return {"error": f"Invalid CIDR '{cidr}': {ve}", "tool": "add_trusted_proxies"}
            trusted = {"source": "static", "ranges": cidr_list}
        patch_resp = await _request("PATCH", f"/config/apps/http/servers/{server_name}/trusted_proxies", json=trusted)
        if patch_resp.status_code == 404:
            put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/trusted_proxies", json=trusted)
            put_resp.raise_for_status()
        else:
            patch_resp.raise_for_status()
        return {"result": {"configured": True, "server": server_name, "trusted_proxies": trusted}}
    except Exception as e:
        err = _err(e, "add_trusted_proxies"); err["server_name"] = server_name; return err


@mcp.tool()
async def delete_trusted_proxies(server_name: str = "") -> dict:
    """Remove trusted proxy IP range configuration from a Caddy HTTP server. After removal, Caddy will no longer trust X-Forwarded-For headers from upstream proxies. server_name: auto-detects first server if empty."""
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        server_name = server_name.strip()
        del_resp = await _request("DELETE", f"/config/apps/http/servers/{server_name}/trusted_proxies")
        if del_resp.status_code not in (200, 204, 404):
            del_resp.raise_for_status()
        return {"result": {"deleted": True, "server": server_name}}
    except Exception as e:
        err = _err(e, "delete_trusted_proxies")
        err["server_name"] = server_name
        return err


@mcp.tool()
async def list_virtual_hosts() -> dict:
    """List all virtual hosts (domain names) configured across all Caddy HTTP servers. Scans every route in every server block and extracts unique hostname values from host matchers. Useful for a quick overview of what domains Caddy is serving and which server block each belongs to."""
    try:
        resp = await _request("GET", "/config/apps/http/servers")
        resp.raise_for_status()
        servers = resp.json() or {}
        hosts: dict[str, list[str]] = {}
        for server_name, server_cfg in servers.items():
            server_hosts: list[str] = []
            for route in server_cfg.get("routes", []):
                for matcher in route.get("match", []):
                    for h in matcher.get("host", []):
                        if h not in server_hosts:
                            server_hosts.append(h)
            if server_hosts:
                hosts[server_name] = server_hosts
        all_hosts = sorted({h for hl in hosts.values() for h in hl})
        return {"result": {"servers": hosts, "all_hosts": all_hosts, "total": len(all_hosts)}}
    except Exception as e:
        return _err(e, "list_virtual_hosts")


@mcp.tool()
async def add_redirect_route(
    host: str,
    target: str = "",
    status_code: int = 301,
    path_prefix: str = "",
    server_name: str = "",
) -> dict:
    """Add a redirect route. If target is empty, redirects HTTP → HTTPS (same host, same URI). target: full URL or {scheme}://{host}{uri} template. status_code: 301 (permanent) or 302 (temporary). path_prefix: optional path to restrict redirect scope."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_redirect_route"}
    if not (100 <= status_code <= 599):
        return {"error": f"status_code must be 100-599, got {status_code}", "tool": "add_redirect_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            servers = resp.json() or {}
            server_name = next(iter(servers), "srv0")
        redirect_to = target.strip() if target.strip() else "https://{http.request.host}{http.request.uri}"
        match: dict = {"host": [host]}
        if path_prefix.strip():
            prefix = path_prefix.strip().rstrip("/")
            if not prefix.startswith("/"):
                prefix = "/" + prefix
            match["path"] = [prefix + "/*", prefix]
        route = {
            "match": [match],
            "handle": [{"handler": "static_response", "status_code": status_code, "headers": {"Location": [redirect_to]}}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "redirect_to": redirect_to, "status_code": status_code, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_redirect_route")
        err["host"] = host
        return err


@mcp.tool()
async def duplicate_route(server_name: str, source_index: int, insert_index: int = -1) -> dict:
    """Copy an existing route to a new position in the route list. source_index: zero-based index of the route to copy. insert_index: position to insert the copy (-1 = append at end). Useful for creating variations of an existing route."""
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        resp.raise_for_status()
        routes = resp.json() or []
        if source_index < 0 or source_index >= len(routes):
            return {"error": f"source_index {source_index} out of range (0–{len(routes) - 1})", "tool": "duplicate_route"}
        import copy
        duplicate = copy.deepcopy(routes[source_index])
        if insert_index < 0 or insert_index >= len(routes):
            routes.append(duplicate)
            new_index = len(routes) - 1
        else:
            routes.insert(insert_index, duplicate)
            new_index = insert_index
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "copied_from": source_index, "inserted_at": new_index, "total_routes": len(routes)}}
    except Exception as e:
        err = _err(e, "duplicate_route"); err["server_name"] = server_name; err["source_index"] = source_index; return err


@mcp.tool()
async def add_forward_auth_route(
    host: str,
    auth_url: str,
    upstream: str,
    copy_headers: str = "",
    server_name: str = "",
) -> dict:
    """Add a forward authentication route. Every request is first sent to auth_url; if it returns 2xx, the request proceeds to upstream. Non-2xx blocks the request. copy_headers: comma-separated header names to copy from the auth response to the upstream request (e.g. 'X-User,X-Role'). Requires the forward_auth Caddy module."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_forward_auth_route"}
    if not auth_url or not auth_url.strip():
        return {"error": "auth_url must not be empty", "tool": "add_forward_auth_route"}
    if not auth_url.strip().startswith(("http://", "https://")):
        return {"error": "auth_url must start with http:// or https://", "tool": "add_forward_auth_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_forward_auth_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_forward_auth_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        headers = [h.strip() for h in copy_headers.split(",") if h.strip()] if copy_headers else []
        forward_auth_handler: dict = {"handler": "forward_auth", "uri": auth_url.strip()}
        if headers:
            forward_auth_handler["copy_headers"] = headers
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "subroute", "routes": [
                {"handle": [forward_auth_handler]},
                {"handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}]},
            ]}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "auth_url": auth_url, "upstream": upstream, "copy_headers": headers}}
    except Exception as e:
        err = _err(e, "add_forward_auth_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def get_metrics() -> dict:
    """Get Caddy Prometheus metrics from the /metrics endpoint. Returns raw metric text in Prometheus exposition format. Requires the metrics module to be loaded (add 'metrics' to the Caddy config or Caddyfile global options)."""
    try:
        resp = await _request("GET", "/metrics")
        resp.raise_for_status()
        lines = resp.text.splitlines()
        metrics: dict[str, str] = {}
        for line in lines:
            if line and not line.startswith("#"):
                parts = line.split(" ", 1)
                if len(parts) == 2:
                    metrics[parts[0]] = parts[1]
        return {"result": {"raw_lines": len(lines), "metrics": metrics}}
    except Exception as e:
        return _err(e, "get_metrics")


@mcp.tool()
async def add_error_handler_route(
    host: str,
    status_codes: str,
    body: str = "",
    content_type: str = "text/html; charset=utf-8",
    server_name: str = "",
) -> dict:
    """Add a custom error handler route for specific HTTP status codes. status_codes: comma-separated codes (e.g. '404,500') or ranges (e.g. '5xx'). body: HTML/text body for the error response. Caddy invokes error handlers when upstream returns matching status codes."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_error_handler_route"}
    if not status_codes or not status_codes.strip():
        return {"error": "status_codes must not be empty", "tool": "add_error_handler_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        codes = [c.strip() for c in status_codes.split(",") if c.strip()]
        error_route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "subroute", "routes": [{
                "match": [{"expression": f"{{http.error.status_code}} in [{', '.join(codes)}]"}],
                "handle": [{"handler": "static_response", "status_code": "{http.error.status_code}",
                             "headers": {"Content-Type": [content_type]}, "body": body}],
            }]}],
            "terminal": True,
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/errors")
        if get_resp.status_code == 200:
            errors_cfg = get_resp.json() or {}
        else:
            errors_cfg = {}
        if "routes" not in errors_cfg:
            errors_cfg["routes"] = []
        errors_cfg["routes"].append(error_route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/errors", json=errors_cfg)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "status_codes": codes}}
    except Exception as e:
        err = _err(e, "add_error_handler_route")
        err["host"] = host
        return err


@mcp.tool()
async def delete_error_handler_route(host: str, server_name: str = "") -> dict:
    """Remove all error handler routes for a specific host from the Caddy error handler config. Clears the entire errors.routes block for that host. Use add_error_handler_route to re-add them."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "delete_error_handler_route"}
    host = host.strip()
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/errors")
        if get_resp.status_code != 200:
            return {"result": {"server": server_name, "host": host, "removed": 0}}
        errors_cfg = get_resp.json() or {}
        routes = errors_cfg.get("routes", [])
        original_count = len(routes)
        errors_cfg["routes"] = [
            r for r in routes
            if host not in [h for m in r.get("match", []) for h in m.get("host", [])]
        ]
        removed = original_count - len(errors_cfg["routes"])
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/errors", json=errors_cfg)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "removed": removed}}
    except Exception as e:
        err = _err(e, "delete_error_handler_route"); err["host"] = host; return err


@mcp.tool()
async def get_admin_config() -> dict:
    """Get Caddy admin API configuration: listen address, TLS settings, and access controls. Useful for verifying the admin endpoint is properly secured."""
    try:
        resp = await _request("GET", "/config/admin")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_admin_config")


@mcp.tool()
async def add_grpc_route(host: str, upstream: str, server_name: str = "", path_prefix: str = "") -> dict:
    """Add a gRPC reverse proxy route. Caddy proxies gRPC (HTTP/2) traffic using h2c transport. upstream: gRPC server address (e.g. 'localhost:50051'). path_prefix: optional path to restrict which gRPC services are proxied (e.g. '/mypackage.MyService/')."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_grpc_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_grpc_route"}
    upstream = upstream.strip()
    if err := _validate_upstream_dial(upstream):
        return {"error": err, "tool": "add_grpc_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        match: dict = {"host": [host.strip()]}
        if path_prefix.strip():
            _p = path_prefix.strip().rstrip("/")
            if not _p.startswith("/"):
                _p = "/" + _p
            match["path"] = [_p + "/*"]
        route = {
            "match": [match],
            "handle": [{
                "handler": "reverse_proxy",
                "transport": {"protocol": "http", "versions": ["h2c", "2"]},
                "upstreams": [{"dial": upstream.strip()}],
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "upstream": upstream, "transport": "h2c/HTTP2", "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_grpc_route"); err["host"] = host; err["upstream"] = upstream; return err


@mcp.tool()
async def add_response_delete_header_route(host: str, header_names: str, server_name: str = "") -> dict:
    """Add a route that strips one or more response headers from all replies to a given host. Useful for removing server fingerprinting headers like 'X-Powered-By', 'Server', 'X-AspNet-Version'. header_names: comma-separated list of header names to delete."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_response_delete_header_route"}
    if not header_names or not header_names.strip():
        return {"error": "header_names must not be empty", "tool": "add_response_delete_header_route"}
    headers_to_delete = [h.strip() for h in header_names.split(",") if h.strip()]
    if not headers_to_delete:
        return {"error": "header_names must contain at least one header name", "tool": "add_response_delete_header_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "headers", "response": {"delete": headers_to_delete}}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "deleted_headers": headers_to_delete, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_response_delete_header_route"); err["host"] = host; return err




@mcp.tool()
async def add_static_file_route(
    host: str,
    root: str,
    browse: bool = False,
    hide_dotfiles: bool = True,
    server_name: str = "",
) -> dict:
    """Add a route that serves static files from a directory on the Caddy server's filesystem. host: virtual host to match. root: absolute path to the directory to serve (e.g. '/var/www/html'). browse: enable directory listing when no index file exists. hide_dotfiles: hide files starting with '.' (default True)."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_static_file_route"}
    if not root or not root.strip():
        return {"error": "root must not be empty", "tool": "add_static_file_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        handler: dict = {"handler": "file_server", "root": root.strip()}
        if browse:
            handler["browse"] = {}
        if hide_dotfiles:
            handler["hide"] = [".*"]
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [handler],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "root": root, "browse": browse, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_static_file_route")
        err["host"] = host
        return err




@mcp.tool()
async def add_response_set_header_route(
    host: str,
    headers: str,
    server_name: str = "",
) -> dict:
    """Add a route that sets security or custom response headers for all replies to a given host. Useful for HSTS, X-Frame-Options, CSP, etc. headers: 'Name: Value' pairs separated by newlines or semicolons (e.g. 'Strict-Transport-Security: max-age=31536000; X-Frame-Options: SAMEORIGIN'). Complements add_response_delete_header_route."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_response_set_header_route"}
    if not headers or not headers.strip():
        return {"error": "headers must not be empty", "tool": "add_response_set_header_route"}
    set_map: dict = {}
    for raw in re.split(r"[;\n]+", headers):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            return {"error": f"Invalid header '{raw}': must be 'Name: Value'", "tool": "add_response_set_header_route"}
        hname, _, hval = raw.partition(":")
        set_map[hname.strip()] = [hval.strip()]
    if not set_map:
        return {"error": "No valid headers parsed", "tool": "add_response_set_header_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "headers", "response": {"set": set_map}}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "set_headers": set_map, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_response_set_header_route")
        err["host"] = host
        return err


@mcp.tool()
async def set_acme_email(email: str) -> dict:
    """Set the global ACME registration email for Let's Encrypt certificate issuance. This email receives expiry warnings from the CA. Applies immediately."""
    if not email or not email.strip():
        return {"error": "email must not be empty", "tool": "set_acme_email"}
    email = email.strip()
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return {"error": f"'{email}' is not a valid email address (expected user@domain.tld)", "tool": "set_acme_email"}
    try:
        resp = await _request("PUT", "/config/apps/tls/automation/email", json=email)
        resp.raise_for_status()
        return {"result": {"email": email, "set": True}}
    except Exception as e:
        return _err(e, "set_acme_email")


@mcp.tool()
async def add_sse_route(
    host: str,
    upstream: str,
    path: str = "",
    server_name: str = "",
) -> dict:
    """Add a Server-Sent Events (SSE) reverse proxy route. Sets flush_interval=-1 to disable buffering so events stream to clients in real time. host: virtual host. upstream: backend SSE server (e.g. 'localhost:8080'). path: optional path prefix to restrict matching (e.g. '/events', '/sse')."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_sse_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_sse_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_sse_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        match: dict = {"host": [host.strip()]}
        if path.strip():
            p = path.strip().rstrip("/")
            if not p.startswith("/"):
                p = "/" + p
            match["path"] = [p + "/*", p] if not p.endswith("*") else [p]
        route = {
            "match": [match],
            "handle": [{
                "handler": "reverse_proxy",
                "flush_interval": -1,
                "upstreams": [{"dial": upstream}],
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "upstream": upstream, "flush_interval": -1, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_sse_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_security_headers_route(
    host: str,
    hsts_max_age: int = 31536000,
    hsts_subdomains: bool = True,
    x_frame_options: str = "SAMEORIGIN",
    x_content_type_options: bool = True,
    referrer_policy: str = "strict-origin-when-cross-origin",
    server_name: str = "",
) -> dict:
    """Add a route that sets a bundle of standard security response headers for a host. Covers HSTS, clickjacking protection, MIME sniffing prevention, and referrer policy. hsts_max_age: HSTS max-age in seconds (default 1 year). x_frame_options: DENY, SAMEORIGIN, or empty to omit. referrer_policy: see MDN for valid values."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_security_headers_route"}
    _VALID_X_FRAME = {"DENY", "SAMEORIGIN", ""}
    if x_frame_options.strip() not in _VALID_X_FRAME:
        return {"error": f"x_frame_options must be 'DENY', 'SAMEORIGIN', or empty (to omit). Got '{x_frame_options}'", "tool": "add_security_headers_route"}
    if hsts_max_age < 0:
        return {"error": "hsts_max_age must be >= 0", "tool": "add_security_headers_route"}
    set_map: dict = {
        "Strict-Transport-Security": [f"max-age={hsts_max_age}" + ("; includeSubDomains" if hsts_subdomains else "")],
        "Referrer-Policy": [referrer_policy.strip()],
    }
    if x_frame_options.strip():
        set_map["X-Frame-Options"] = [x_frame_options.strip()]
    if x_content_type_options:
        set_map["X-Content-Type-Options"] = ["nosniff"]
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "headers", "response": {"set": set_map}}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "headers_set": list(set_map.keys()), "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_security_headers_route")
        err["host"] = host
        return err


@mcp.tool()
async def add_cache_headers_route(
    host: str,
    path_pattern: str,
    max_age: int = 86400,
    immutable: bool = False,
    server_name: str = "",
) -> dict:
    """Add a route that sets Cache-Control response headers for static assets matching a path pattern. path_pattern: URL path glob (e.g. '/static/*', '*.css', '/assets/*'). max_age: seconds to cache (default 86400 = 1 day). immutable: add 'immutable' directive for versioned assets that never change."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_cache_headers_route"}
    if not path_pattern or not path_pattern.strip():
        return {"error": "path_pattern must not be empty", "tool": "add_cache_headers_route"}
    if not path_pattern.strip().startswith("/"):
        return {"error": "path_pattern must start with '/' (e.g. '/static/*', '/assets/*.css')", "tool": "add_cache_headers_route"}
    if max_age < 0:
        return {"error": "max_age must be >= 0", "tool": "add_cache_headers_route"}
    cc_value = f"public, max-age={max_age}"
    if immutable:
        cc_value += ", immutable"
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()], "path": [path_pattern.strip()]}],
            "handle": [{"handler": "headers", "response": {"set": {"Cache-Control": [cc_value]}}}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "path_pattern": path_pattern, "cache_control": cc_value, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_cache_headers_route")
        err["host"] = host
        return err


@mcp.tool()
async def add_retry_route(
    host: str,
    upstream: str,
    retries: int = 3,
    max_fails: int = 3,
    fail_duration: int = 10,
    server_name: str = "",
) -> dict:
    """Add a reverse proxy route with automatic retry on 502/503/504 responses and passive health checking. retries: number of times to retry failed requests. max_fails: consecutive failures before marking upstream unhealthy. fail_duration: seconds to consider an upstream unhealthy after max_fails. Unhealthy upstreams are retried after fail_duration passes."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_retry_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_retry_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_retry_route"}
    if retries < 1:
        return {"error": "retries must be >= 1", "tool": "add_retry_route"}
    if max_fails < 1:
        return {"error": "max_fails must be >= 1", "tool": "add_retry_route"}
    if fail_duration < 1:
        return {"error": "fail_duration must be >= 1 second", "tool": "add_retry_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream}],
                "load_balancing": {
                    "retries": retries,
                    "retry_match": [{"status_code": [502, 503, 504]}],
                },
                "health_checks": {
                    "passive": {
                        "fail_duration": f"{fail_duration}s",
                        "max_fails": max_fails,
                        "unhealthy_status": [502, 503, 504],
                    }
                },
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "upstream": upstream, "retries": retries, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_retry_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_strip_prefix_route(
    host: str,
    path_prefix: str,
    upstream: str,
    server_name: str = "",
) -> dict:
    """Add a route that strips a path prefix before proxying — for services deployed at a subpath that expect requests at '/'. Example: strip '/myapp' so '/myapp/api/v1' becomes '/api/v1' at the upstream. path_prefix: the prefix to remove (e.g. '/myapp'). Requests not matching the prefix fall through."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_strip_prefix_route"}
    if not path_prefix or not path_prefix.strip():
        return {"error": "path_prefix must not be empty", "tool": "add_strip_prefix_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_strip_prefix_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_strip_prefix_route"}
    prefix = path_prefix.strip().rstrip("/")
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()], "path": [prefix + "/*", prefix]}],
            "handle": [
                {"handler": "rewrite", "strip_path_prefix": prefix},
                {"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]},
            ],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "prefix": prefix, "upstream": upstream, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_strip_prefix_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_header_match_route(
    host: str,
    header_name: str,
    header_value: str,
    upstream: str,
    server_name: str = "",
) -> dict:
    """Add a route that only matches requests containing a specific header value, proxying to a different upstream. Useful for canary deployments, A/B testing, or internal routing. header_name: request header to match (e.g. 'X-Canary'). header_value: exact value to match. Non-matching requests fall through to other routes."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_header_match_route"}
    if not header_name or not header_name.strip():
        return {"error": "header_name must not be empty", "tool": "add_header_match_route"}
    if not header_value or not header_value.strip():
        return {"error": "header_value must not be empty", "tool": "add_header_match_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_header_match_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_header_match_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()], "header": {header_name.strip(): [header_value.strip()]}}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.insert(0, route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "match_header": f"{header_name}: {header_value}", "upstream": upstream, "route_index": 0}}
    except Exception as e:
        err = _err(e, "add_header_match_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_method_match_route(
    host: str,
    methods: str,
    upstream: str,
    server_name: str = "",
    path_prefix: str = "",
) -> dict:
    """Add a route that only matches specific HTTP methods (e.g. 'GET,POST'), proxying to a different upstream. Useful for routing API writes to one backend and reads to another, or blocking specific methods. methods: comma-separated list (e.g. 'POST,PUT,PATCH,DELETE'). path_prefix: optional path filter (e.g. '/api'). Non-matching requests fall through to other routes. Inserted at index 0 for priority."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_method_match_route"}
    if not methods or not methods.strip():
        return {"error": "methods must not be empty", "tool": "add_method_match_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_method_match_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_method_match_route"}
    _VALID_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
    method_list = [m.strip().upper() for m in methods.split(",") if m.strip()]
    if not method_list:
        return {"error": "methods must contain at least one valid HTTP method", "tool": "add_method_match_route"}
    invalid = [m for m in method_list if m not in _VALID_HTTP_METHODS]
    if invalid:
        return {"error": f"Invalid HTTP methods: {', '.join(invalid)}. Valid: {', '.join(sorted(_VALID_HTTP_METHODS))}", "tool": "add_method_match_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        match: dict = {"host": [host.strip()], "method": method_list}
        if path_prefix and path_prefix.strip():
            prefix = path_prefix.strip().rstrip("/")
            if not prefix.startswith("/"):
                prefix = "/" + prefix
            match["path"] = [prefix + "/*", prefix]
        route = {
            "match": [match],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream.strip()}]}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.insert(0, route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "methods": method_list, "upstream": upstream, "route_index": 0}}
    except Exception as e:
        err = _err(e, "add_method_match_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_query_match_route(
    host: str,
    query_param: str,
    query_value: str,
    upstream: str,
    server_name: str = "",
) -> dict:
    """Add a route that matches requests containing a specific URL query parameter value, proxying to a different upstream. Useful for feature flags, debug routing, or versioned API dispatch. query_param: URL query key (e.g. 'version'). query_value: value to match (e.g. 'v2'). Non-matching requests fall through to other routes. Inserted at index 0 for priority."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_query_match_route"}
    if not query_param or not query_param.strip():
        return {"error": "query_param must not be empty", "tool": "add_query_match_route"}
    if not query_value or not query_value.strip():
        return {"error": "query_value must not be empty", "tool": "add_query_match_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_query_match_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_query_match_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()], "query": {query_param.strip(): [query_value.strip()]}}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.insert(0, route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "match_query": f"{query_param}={query_value}", "upstream": upstream, "route_index": 0}}
    except Exception as e:
        err = _err(e, "add_query_match_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_request_body_limit(
    host: str,
    max_size_bytes: int,
    server_name: str = "",
) -> dict:
    """Add a request body size limit for a host. Requests with bodies exceeding max_size_bytes will be rejected with 413 Payload Too Large before reaching the upstream. Useful for protecting API endpoints from oversized uploads. Inserted at index 0 so it applies before other routes. Common values: 1048576 (1MB), 10485760 (10MB), 104857600 (100MB)."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_request_body_limit"}
    if max_size_bytes <= 0:
        return {"error": "max_size_bytes must be greater than 0", "tool": "add_request_body_limit"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "request_body", "max_size": max_size_bytes}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.insert(0, route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "max_size_bytes": max_size_bytes, "route_index": 0}}
    except Exception as e:
        err = _err(e, "add_request_body_limit")
        err["host"] = host
        return err


@mcp.tool()
async def enable_server_access_log(
    server_name: str,
    output_file: str = "",
    format: str = "json",
) -> dict:
    """Enable HTTP access logging for a Caddy server, writing one log line per request. output_file: path to write logs (e.g. '/var/log/caddy/access.log'); if empty, logs go to stderr. format: 'json' (structured, default) or 'console' (human-readable). Use list_servers to find server names."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "enable_server_access_log"}
    server_name = server_name.strip()
    valid_formats = {"json", "console"}
    if format not in valid_formats:
        return {"error": f"format must be one of: {', '.join(sorted(valid_formats))}", "tool": "enable_server_access_log"}
    try:
        encoder: dict = {"format": format}
        if output_file and output_file.strip():
            writer: dict = {"output": "file", "filename": output_file.strip(), "roll_size_mb": 100, "roll_keep": 10}
        else:
            writer = {"output": "stderr"}
        # Step 1: configure the global named log sink at /config/logging/logs/access
        log_sink = {"writer": writer, "encoder": encoder}
        sink_resp = await _request("PUT", "/config/logging/logs/access", json=log_sink)
        sink_resp.raise_for_status()
        # Step 2: point the server's access log at that named sink
        server_logs = {"default_logger_name": "access"}
        resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/logs", json=server_logs)
        resp.raise_for_status()
        return {"result": {"server": server_name, "format": format, "output": output_file or "stderr", "enabled": True}}
    except Exception as e:
        err = _err(e, "enable_server_access_log")
        err["server_name"] = server_name
        return err


@mcp.tool()
async def disable_server_access_log(server_name: str) -> dict:
    """Disable HTTP access logging for a Caddy server, removing the log configuration entirely. Use list_servers to find server names. Use enable_server_access_log to re-enable."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "disable_server_access_log"}
    server_name = server_name.strip()
    try:
        resp = await _request("DELETE", f"/config/apps/http/servers/{server_name}/logs")
        resp.raise_for_status()
        return {"result": {"server": server_name, "access_log_disabled": True}}
    except Exception as e:
        err = _err(e, "disable_server_access_log"); err["server_name"] = server_name; return err


@mcp.tool()
async def add_cookie_match_route(
    host: str,
    cookie_name: str,
    cookie_value: str,
    upstream: str,
    server_name: str = "",
) -> dict:
    """Add a route that matches requests containing a specific cookie value, proxying to a different upstream. Useful for sticky sessions, A/B testing, feature flags, or canary deployments. cookie_name: cookie key to match. cookie_value: exact value to match. Non-matching requests fall through to other routes. Inserted at index 0 for priority."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_cookie_match_route"}
    if not cookie_name or not cookie_name.strip():
        return {"error": "cookie_name must not be empty", "tool": "add_cookie_match_route"}
    if not cookie_value or not cookie_value.strip():
        return {"error": "cookie_value must not be empty", "tool": "add_cookie_match_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_cookie_match_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_cookie_match_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()], "header_regexp": {"Cookie": f"(?:^|;\\s*){re.escape(cookie_name.strip())}={re.escape(cookie_value.strip())}(?:;|$)"}}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.insert(0, route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "cookie": f"{cookie_name}={cookie_value}", "upstream": upstream, "route_index": 0}}
    except Exception as e:
        err = _err(e, "add_cookie_match_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_not_found_route(
    host: str,
    body: str = "404 Not Found",
    content_type: str = "text/plain; charset=utf-8",
    server_name: str = "",
) -> dict:
    """Add a catch-all route that returns 404 for a host, with a custom response body. Useful for explicitly handling unmatched paths instead of relying on Caddy's default 404. Appended at end so other routes still match first. body: response body text or JSON. content_type: response Content-Type header."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_not_found_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{"handler": "static_response", "status_code": 404, "headers": {"Content-Type": [content_type]}, "body": body}],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "status_code": 404, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_not_found_route")
        err["host"] = host
        return err


@mcp.tool()
async def add_circuit_breaker_route(
    host: str,
    upstream: str,
    server_name: str = "",
    path_prefix: str = "",
    max_fails: int = 3,
    fail_duration: str = "30s",
    unhealthy_latency: str = "10s",
) -> dict:
    """Add a reverse proxy route with passive circuit breaking: upstream is automatically marked unhealthy after max_fails failures within fail_duration, and recovers automatically. Requests to an unhealthy upstream are dropped until the circuit resets. max_fails: consecutive failures before marking unhealthy (default 3). fail_duration: window for counting failures (e.g. '30s', '1m'). unhealthy_latency: response latency threshold that counts as a failure (e.g. '10s')."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_circuit_breaker_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_circuit_breaker_route"}
    upstream = upstream.strip()
    if err := _validate_upstream_dial(upstream):
        return {"error": err, "tool": "add_circuit_breaker_route"}
    if max_fails < 1:
        return {"error": "max_fails must be at least 1", "tool": "add_circuit_breaker_route"}
    if err := _validate_go_duration(fail_duration, "fail_duration"):
        return {"error": err, "tool": "add_circuit_breaker_route"}
    if err := _validate_go_duration(unhealthy_latency, "unhealthy_latency"):
        return {"error": err, "tool": "add_circuit_breaker_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        match: dict = {"host": [host.strip()]}
        if path_prefix and path_prefix.strip():
            prefix = path_prefix.strip().rstrip("/")
            if not prefix.startswith("/"):
                prefix = "/" + prefix
            match["path"] = [prefix + "/*", prefix]
        route = {
            "match": [match],
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream.strip()}],
                "health_checks": {
                    "passive": {
                        "fail_duration": fail_duration,
                        "max_fails": max_fails,
                        "unhealthy_status": [500, 502, 503, 504],
                        "unhealthy_latency": unhealthy_latency,
                    }
                },
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "upstream": upstream, "max_fails": max_fails, "fail_duration": fail_duration, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_circuit_breaker_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_active_health_check_route(
    host: str,
    upstream: str,
    health_path: str = "/health",
    interval: str = "30s",
    timeout: str = "5s",
    expect_status: int = 200,
    server_name: str = "",
) -> dict:
    """Add a reverse proxy route with active health checking: Caddy periodically polls the upstream at health_path and stops sending traffic if it fails. Distinct from passive circuit breaking — active checks run independently of traffic. health_path: path to poll (e.g. '/health', '/ping'). interval: poll frequency (e.g. '30s', '1m'). timeout: max wait for health response. expect_status: expected HTTP status (default 200)."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_active_health_check_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_active_health_check_route"}
    upstream = upstream.strip()
    err = _validate_upstream_dial(upstream)
    if err:
        return {"error": err, "tool": "add_active_health_check_route"}
    if expect_status < 100 or expect_status > 599:
        return {"error": "expect_status must be a valid HTTP status code (100-599)", "tool": "add_active_health_check_route"}
    if err := _validate_go_duration(interval, "interval"):
        return {"error": err, "tool": "add_active_health_check_route"}
    if err := _validate_go_duration(timeout, "timeout"):
        return {"error": err, "tool": "add_active_health_check_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream}],
                "health_checks": {
                    "active": {
                        "path": health_path.strip() if health_path.strip().startswith("/") else "/" + health_path.strip(),
                        "interval": interval,
                        "timeout": timeout,
                        "expect_status": expect_status,
                    }
                },
            }],
        }
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        routes = (get_resp.json() or []) if get_resp.status_code == 200 else []
        routes.append(route)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=routes)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "upstream": upstream, "health_path": health_path, "interval": interval, "route_index": len(routes) - 1}}
    except Exception as e:
        err = _err(e, "add_active_health_check_route")
        err["host"] = host
        err["upstream"] = upstream
        return err


@mcp.tool()
async def add_ip_denylist_route(
    host: str,
    blocked_cidrs: str,
    upstream: str = "",
    server_name: str = "",
) -> dict:
    """Add a route that blocks specific IP addresses/CIDRs with 403, while allowing all other traffic. Inverse of add_ip_filter_route (which is an allowlist). host: domain to match. blocked_cidrs: comma-separated IPs or CIDRs to deny (e.g. '1.2.3.4,10.0.0.0/8'). upstream: if provided, proxy non-blocked traffic here; otherwise, non-blocked traffic passes through Caddy normally."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_ip_denylist_route"}
    if not blocked_cidrs or not blocked_cidrs.strip():
        return {"error": "blocked_cidrs must not be empty", "tool": "add_ip_denylist_route"}
    cidr_list = [c.strip() for c in blocked_cidrs.split(",") if c.strip()]
    if not cidr_list:
        return {"error": "blocked_cidrs must contain at least one IP or CIDR", "tool": "add_ip_denylist_route"}
    try:
        for cidr in cidr_list:
            ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        return {"error": f"Invalid IP/CIDR: {e}", "tool": "add_ip_denylist_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/apps/http/servers")
            resp.raise_for_status()
            server_name = next(iter(resp.json() or {}), "srv0")
        deny_route = {
            "match": [{"host": [host.strip()], "remote_ip": {"ranges": cidr_list}}],
            "handle": [{"handler": "static_response", "status_code": 403, "body": "Forbidden"}],
        }
        new_routes = [deny_route]
        if upstream and upstream.strip():
            upstream = upstream.strip()
            up_err = _validate_upstream_dial(upstream)
            if up_err:
                return {"error": up_err, "tool": "add_ip_denylist_route"}
            allow_route = {
                "match": [{"host": [host.strip()]}],
                "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
            }
            new_routes.append(allow_route)
        get_resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes")
        existing = (get_resp.json() or []) if get_resp.status_code == 200 else []
        existing.extend(new_routes)
        put_resp = await _request("PUT", f"/config/apps/http/servers/{server_name}/routes", json=existing)
        put_resp.raise_for_status()
        return {"result": {"server": server_name, "host": host, "blocked_cidrs": cidr_list, "upstream": upstream or "(none)", "routes_added": len(new_routes)}}
    except Exception as e:
        err = _err(e, "add_ip_denylist_route")
        err["host"] = host
        return err


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
