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
    if not config_path.startswith("/"):
        return {"error": "config_path must start with '/'", "tool": "get_config_path"}
    try:
        resp = await _request("GET", f"/config{config_path}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_config_path")


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
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name.strip()}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_server")


@mcp.tool()
async def delete_server(server_name: str) -> dict:
    """Delete a Caddy HTTP server block entirely, removing all its routes and configuration. This is irreversible without a config backup."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "delete_server"}
    try:
        resp = await _request("DELETE", f"/config/apps/http/servers/{server_name.strip()}")
        resp.raise_for_status()
        return {"result": {"deleted": True, "name": server_name.strip()}}
    except Exception as e:
        return _err(e, "delete_server")


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
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        config = resp.json()

        servers = config.get("apps", {}).get("http", {}).get("servers", {})
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
async def get_route(server_name: str, route_index: int) -> dict:
    """Get the full configuration of a specific route by index. Use list_routes to find server_name and index."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "get_route"}
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "get_route"}
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes/{route_index}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_route")


@mcp.tool()
async def add_reverse_proxy_route(host: str, upstream: str, server_name: str = "") -> dict:
    """Add a host-based reverse proxy route to Caddy. host: domain (e.g. 'app.example.com'). upstream: backend dial address (e.g. 'localhost:3000'). server_name: Caddy server block (auto-detects first server if empty)."""
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
async def add_path_route(path: str, upstream: str, server_name: str = "") -> dict:
    """Add a path-based reverse proxy route. All requests matching the URL path prefix are proxied to upstream regardless of hostname. path: e.g. '/api/*' or '/v2/*'. upstream: backend address (e.g., 'localhost:8080'). server_name: auto-detects first server if empty."""
    if not path or not path.strip():
        return {"error": "path must not be empty", "tool": "add_path_route"}
    if not upstream or not upstream.strip():
        return {"error": "upstream must not be empty", "tool": "add_path_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/")
            resp.raise_for_status()
            config = resp.json() or {}
            servers = config.get("apps", {}).get("http", {}).get("servers", {})
            if not servers:
                return {"error": "No HTTP servers found in Caddy config. Use reload to load an initial configuration first.", "tool": "add_path_route"}
            server_name = next(iter(servers))

        route = {
            "match": [{"path": [path]}],
            "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": upstream}]}],
        }
        resp = await _request("POST", f"/config/apps/http/servers/{server_name}/routes", json=route)
        resp.raise_for_status()
        return {"result": {"added": True, "path": path, "upstream": upstream, "server": server_name}}
    except Exception as e:
        return _err(e, "add_path_route")


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
async def add_redirect(from_host: str, to_url: str, status_code: int = 301, server_name: str = "") -> dict:
    """Add an HTTP redirect route. from_host: domain to redirect (e.g. 'old.example.com'). to_url: destination URL. status_code: 301 (permanent), 302 (temporary), 307, or 308."""
    if not from_host or not from_host.strip():
        return {"error": "from_host must not be empty", "tool": "add_redirect"}
    if not to_url or not to_url.strip():
        return {"error": "to_url must not be empty", "tool": "add_redirect"}
    if status_code not in {301, 302, 307, 308}:
        return {"error": "status_code must be 301, 302, 307, or 308", "tool": "add_redirect"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/")
            resp.raise_for_status()
            config = resp.json() or {}
            servers = config.get("apps", {}).get("http", {}).get("servers", {})
            if not servers:
                return {"error": "No HTTP servers configured in Caddy", "tool": "add_redirect"}
            server_name = next(iter(servers))

        route = {
            "match": [{"host": [from_host]}],
            "handle": [{
                "handler": "static_response",
                "status_code": status_code,
                "headers": {"Location": [to_url]},
            }],
        }
        resp = await _request("POST", f"/config/apps/http/servers/{server_name}/routes", json=route)
        resp.raise_for_status()
        return {"result": {"added": True, "from": from_host, "to": to_url, "status_code": status_code, "server": server_name}}
    except Exception as e:
        return _err(e, "add_redirect")


@mcp.tool()
async def add_header_route(host: str, header_name: str, header_value: str, server_name: str = "") -> dict:
    """Add a route that injects a response header for all requests to a given host. Useful for HSTS, CORS, X-Frame-Options, X-Content-Type-Options, etc. host: domain to match. server_name: auto-detects first server if empty."""
    if not host or not host.strip():
        return {"error": "host must not be empty", "tool": "add_header_route"}
    if not header_name or not header_name.strip():
        return {"error": "header_name must not be empty", "tool": "add_header_route"}
    if not header_value:
        return {"error": "header_value must not be empty", "tool": "add_header_route"}
    try:
        if not server_name:
            resp = await _request("GET", "/config/")
            resp.raise_for_status()
            config = resp.json() or {}
            servers = config.get("apps", {}).get("http", {}).get("servers", {})
            if not servers:
                return {"error": "No HTTP servers found. Use create_server first.", "tool": "add_header_route"}
            server_name = next(iter(servers))

        route = {
            "match": [{"host": [host.strip()]}],
            "handle": [{
                "handler": "headers",
                "response": {"set": {header_name.strip(): [header_value]}},
            }],
        }
        resp = await _request("POST", f"/config/apps/http/servers/{server_name}/routes", json=route)
        resp.raise_for_status()
        return {"result": {"added": True, "host": host.strip(), "header": header_name.strip(), "server": server_name}}
    except Exception as e:
        return _err(e, "add_header_route")


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



@mcp.tool()
async def create_server(name: str, listen_addresses: str) -> dict:
    """Create a new Caddy HTTP server block. name: server identifier used in other tools (e.g., 'srv1'). listen_addresses: comma-separated bind addresses (e.g., ':80' or ':80,:443' or '0.0.0.0:8080'). The new server starts with no routes — use add_reverse_proxy_route or similar to add routes."""
    if not name or not name.strip():
        return {"error": "name must not be empty", "tool": "create_server"}
    if not listen_addresses or not listen_addresses.strip():
        return {"error": "listen_addresses must not be empty", "tool": "create_server"}
    listen = [a.strip() for a in listen_addresses.split(",") if a.strip()]
    try:
        resp = await _request(
            "PATCH",
            f"/config/apps/http/servers/{name.strip()}",
            json={"listen": listen, "routes": []},
        )
        resp.raise_for_status()
        return {"result": {"created": True, "name": name.strip(), "listen": listen}}
    except Exception as e:
        return _err(e, "create_server")


@mcp.tool()
async def add_tls_policy(subjects: str, ca_url: str = "", email: str = "") -> dict:
    """Add a TLS automation policy for one or more domains. subjects: comma-separated domain names (e.g., 'example.com,*.example.com'). ca_url: ACME CA directory URL (defaults to Let's Encrypt if empty). email: ACME account email. Appends to existing TLS policies."""
    if not subjects or not subjects.strip():
        return {"error": "subjects must not be empty", "tool": "add_tls_policy"}
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
        resp = await _request("GET", "/config/")
        resp.raise_for_status()
        config = resp.json() or {}

        tls_automation = (
            config.get("apps", {})
            .get("tls", {})
            .get("automation", {})
        )
        policies = tls_automation.get("policies", [])
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
        return _err(e, "add_tls_policy")


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
async def delete_tls_policy(policy_index: int) -> dict:
    """Delete a TLS automation policy by its 0-based index. Use list_tls_policies to see indices. Changes take effect immediately."""
    if policy_index < 0:
        return {"error": "policy_index must be >= 0", "tool": "delete_tls_policy"}
    try:
        resp = await _request("DELETE", f"/config/apps/tls/automation/policies/{policy_index}")
        resp.raise_for_status()
        return {"result": {"deleted": True, "index": policy_index}}
    except Exception as e:
        return _err(e, "delete_tls_policy")


@mcp.tool()
async def update_route(server_name: str, route_index: int, config_json: str) -> dict:
    """Replace a route in-place by index. config_json: full route JSON object. Use get_route to retrieve the current config, modify it, then pass it here. Changes take effect immediately."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "update_route"}
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "update_route"}
    if not config_json or not config_json.strip():
        return {"error": "config_json must not be empty", "tool": "update_route"}
    try:
        route_config = json.loads(config_json)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "tool": "update_route"}
    try:
        resp = await _request(
            "PATCH",
            f"/config/apps/http/servers/{server_name}/routes/{route_index}",
            json=route_config,
        )
        resp.raise_for_status()
        return {"result": {"updated": True, "server_name": server_name, "route_index": route_index}}
    except Exception as e:
        return _err(e, "update_route")


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
    if not listen_addresses or not listen_addresses.strip():
        return {"error": "listen_addresses must not be empty", "tool": "update_listen_addresses"}
    listen = [a.strip() for a in listen_addresses.split(",") if a.strip()]
    try:
        resp = await _request(
            "PATCH",
            f"/config/apps/http/servers/{server_name.strip()}/listen",
            json=listen,
        )
        resp.raise_for_status()
        return {"result": {"updated": True, "name": server_name.strip(), "listen": listen}}
    except Exception as e:
        return _err(e, "update_listen_addresses")


@mcp.tool()
async def get_pki_ca(ca_name: str) -> dict:
    """Get details for a Caddy PKI certificate authority: root cert, intermediate cert PEM, signing policy, and lifetime. Use list_pki_cas to find CA names. Default local CA is named 'local'."""
    if not ca_name or not ca_name.strip():
        return {"error": "ca_name must not be empty", "tool": "get_pki_ca"}
    try:
        resp = await _request("GET", f"/pki/ca/{ca_name.strip()}")
        resp.raise_for_status()
        return {"result": resp.json()}
    except Exception as e:
        return _err(e, "get_pki_ca")



@mcp.tool()
async def update_upstream(server_name: str, route_index: int, new_upstream: str) -> dict:
    """Update the backend dial address for a reverse proxy route. Fetches the route, replaces all upstream dial addresses, and PATCHes in-place. Use list_routes to find server_name and route_index. new_upstream: e.g. 'localhost:8080' or '10.0.0.5:3000'."""
    if not server_name or not server_name.strip():
        return {"error": "server_name must not be empty", "tool": "update_upstream"}
    if route_index < 0:
        return {"error": "route_index must be >= 0", "tool": "update_upstream"}
    if not new_upstream or not new_upstream.strip():
        return {"error": "new_upstream must not be empty", "tool": "update_upstream"}
    try:
        resp = await _request("GET", f"/config/apps/http/servers/{server_name}/routes/{route_index}")
        resp.raise_for_status()
        route = resp.json()

        modified = False
        for handle in route.get("handle", []):
            if handle.get("handler") == "reverse_proxy":
                handle["upstreams"] = [{"dial": new_upstream.strip()}]
                modified = True

        if not modified:
            return {"error": f"Route {route_index} has no reverse_proxy handler; use update_route for other handler types", "tool": "update_upstream"}

        patch_resp = await _request(
            "PATCH",
            f"/config/apps/http/servers/{server_name}/routes/{route_index}",
            json=route,
        )
        patch_resp.raise_for_status()
        return {"result": {"updated": True, "server": server_name, "route_index": route_index, "upstream": new_upstream.strip()}}
    except Exception as e:
        return _err(e, "update_upstream")



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
async def stop_caddy() -> dict:
    """Gracefully stop the Caddy server process. WARNING: this terminates the Caddy admin API — the server will be unreachable until restarted externally."""
    try:
        resp = await _request("POST", "/stop")
        resp.raise_for_status()
        return {"result": {"stopped": True}}
    except Exception as e:
        return _err(e, "stop_caddy")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
