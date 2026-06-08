# mcp-caddy

MCP server for [Caddy](https://caddyserver.com/) web server management. It wraps
the Caddy admin API and exposes **93 tools** for inspecting and managing
servers, routes, reverse proxies, TLS automation, PKI, logging, and the running
configuration — straight from an MCP client like Claude.

## Quick Start

**With uvx (recommended):**
```bash
CADDY_HOST=http://localhost:2019 uvx mcp-caddy
```

**With Docker:**
```bash
docker run -i \
  -e CADDY_HOST=http://localhost:2019 \
  ghcr.io/aaronckj/mcp-caddy:latest
```

**Add to Claude Code:**
```bash
claude mcp add caddy -- uvx mcp-caddy
```

To pass configuration, add `-e` flags before the `--`:
```bash
claude mcp add caddy -e CADDY_HOST=http://localhost:2019 -- uvx mcp-caddy
```

**`mcp.json` snippet** (Claude Desktop / any MCP client):
```json
{
  "mcpServers": {
    "caddy": {
      "command": "uvx",
      "args": ["mcp-caddy"],
      "env": {
        "CADDY_HOST": "http://localhost:2019"
      }
    }
  }
}
```

## Configuration

All configuration is environment-driven. Defaults are safe and point at
localhost only — no real infrastructure is assumed.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CADDY_HOST` | No | `http://localhost:2019` | Caddy admin API URL |
| `CADDY_TIMEOUT` | No | `30` | HTTP timeout in seconds |
| `CADDY_USERNAME` | No | _(unset)_ | HTTP Basic auth username for the admin API |
| `CADDY_PASSWORD` | No | _(unset)_ | HTTP Basic auth password for the admin API |
| `VAULT_PROXY_URL` | No | _(unset)_ | If set, all admin-API calls are routed through a [vaultproxy](https://pypi.org/project/vaultproxy/) instance instead of connecting directly |
| `VAULT_PROXY_SERVICE` | No | `caddy` | Service name used in vaultproxy requests |
| `VAULT_PROXY_CALLER_ID` | No | `mcp-caddy` | Caller id sent as `X-Caller-Id` to vaultproxy |

### Authentication

- **Direct + Basic auth:** set `CADDY_USERNAME` and `CADDY_PASSWORD`. Both must be
  set for auth to be applied; otherwise requests are sent unauthenticated.
- **Via vaultproxy:** set `VAULT_PROXY_URL`. When present it takes precedence over
  the direct connection, and credentials are resolved by vaultproxy rather than
  passed here.

## Finding Your CADDY_HOST

The Caddy admin API binds to `localhost:2019` by default. Depending on your setup:

1. **MCP server on same host as Caddy** -> use the default `http://localhost:2019`
2. **Caddy in Docker, MCP server on the same Docker host** -> add
   `ports: ["127.0.0.1:2019:2019"]` to your Caddy compose service, then use
   `http://localhost:2019`
3. **Remote Caddy host** -> SSH tunnel:
   `ssh -L 2019:localhost:2019 user@caddy.example`, then use `http://localhost:2019`
4. **Caddy exposes admin via reverse proxy** -> set
   `CADDY_HOST=https://caddy-admin.example.com` and supply `CADDY_USERNAME` /
   `CADDY_PASSWORD` if it's protected with Basic auth.

The Caddy admin API has no authentication by default. If you expose it beyond
localhost, protect that route (Caddy basic auth, mTLS, or a vaultproxy) and
configure the matching env vars above.

## Tools

93 tools, grouped by area. Tools marked **destructive** mutate or remove live
configuration — see the warning below.

### Inspection & status (read-only)

| Tool | Description |
|------|-------------|
| `server_info` | Caddy version and loaded modules |
| `server_version` | Version of this MCP server itself |
| `health_check` | Liveness probe for this MCP server |
| `get_config` | Full configuration as JSON |
| `get_config_path` | A single config node by path |
| `get_admin_config` | Admin API listen/TLS/access settings |
| `list_modules` | Modules loaded in the running server |
| `get_metrics` | Prometheus metrics from `/metrics` |
| `list_servers` | HTTP server blocks with listen addrs and route counts |
| `get_server` | Full config of one HTTP server block |
| `list_routes` | All routes with hosts, handler, upstreams, index |
| `get_route` | A single route by server + index |
| `get_routes_by_host` | All routes matching a hostname (read-only) |
| `list_virtual_hosts` | All configured domain names |
| `list_upstreams` | Reverse-proxy upstream health and request counts |

### Server management

| Tool | Description |
|------|-------------|
| `create_server` | Create a new HTTP server block |
| `delete_server` | **destructive** — delete a server block and all its routes |
| `update_listen_addresses` | Change listen addrs without touching routes |
| `get_server_timeouts` | Read request/response timeouts |
| `set_server_timeouts` | Configure request/response timeouts |
| `enable_server_access_log` | Turn on per-request access logging |
| `disable_server_access_log` | **destructive** — remove access-log config |

### Reverse-proxy routes

| Tool | Description |
|------|-------------|
| `add_reverse_proxy_route` | Proxy a host to a backend |
| `add_path_route` | Proxy by URL path prefix regardless of host |
| `add_load_balanced_route` | Proxy across multiple backends |
| `add_active_health_check_route` | Proxy with active health checks |
| `add_circuit_breaker_route` | Proxy with passive circuit breaking |
| `add_retry_route` | Proxy with automatic retry on 502/503/504 |
| `add_grpc_route` | gRPC reverse proxy |
| `add_websocket_route` | WebSocket-optimized reverse proxy |
| `add_sse_route` | Server-Sent Events reverse proxy |
| `add_php_fastcgi_route` | PHP-FPM FastCGI route |
| `update_upstream` | Change the backend dial address of a route |
| `mark_upstream_health` | Manually override an upstream's health state |

### Static, redirect & response routes

| Tool | Description |
|------|-------------|
| `add_static_file_server` | Serve files from a directory |
| `add_static_file_route` | Serve static files for a host/path |
| `add_try_files_route` | Static + try_files fallback (SPA pattern) |
| `add_stub_response_route` | Fixed response with no backend |
| `add_not_found_route` | Catch-all 404 with custom body |
| `add_maintenance_route` | 503 maintenance mode for a host |
| `add_error_handler_route` | Custom error handler for status codes |
| `delete_error_handler_route` | **destructive** — remove error handlers for a host |
| `add_redirect` | HTTP redirect (301/302/307/308) |
| `add_redirect_route` | Redirect route |
| `add_https_redirect` | Permanent HTTP->HTTPS redirect |
| `delete_https_redirect` | **destructive** — remove the HTTP->HTTPS redirect |
| `add_rewrite_route` | Strip a path prefix before proxying |
| `add_strip_prefix_route` | Serve a service deployed at a subpath |

### Headers

| Tool | Description |
|------|-------------|
| `add_header_route` | Inject a response header for a host |
| `add_request_header_route` | Inject a request header for a host |
| `add_response_set_header_route` | Set response headers for a host |
| `add_response_delete_header_route` | Strip response headers for a host |
| `add_cache_headers_route` | Set Cache-Control for static assets |
| `add_security_headers_route` | Bundle of standard security headers |
| `add_cors_route` | CORS handling for a host |
| `add_global_headers` | Response headers for ALL requests (no host matcher) |
| `delete_global_headers` | **destructive** — remove global header routes |

### Request matchers

| Tool | Description |
|------|-------------|
| `add_header_match_route` | Match on a request header value |
| `add_cookie_match_route` | Match on a cookie value |
| `add_query_match_route` | Match on a query-parameter value |
| `add_method_match_route` | Match on HTTP method(s) |

### Auth & security

| Tool | Description |
|------|-------------|
| `add_basicauth_route` | HTTP Basic Auth-protected route |
| `add_forward_auth_route` | Forward-auth (external authorizer) route |
| `add_ip_filter_route` | Allowlist: only listed IPs/CIDRs reach the backend |
| `add_ip_denylist_route` | Denylist: block listed IPs/CIDRs with 403 |
| `add_request_body_limit` | Cap request body size for a host |
| `add_compress_route` | Enable gzip/zstd response compression |
| `add_trusted_proxies` | Configure trusted proxy IP ranges |
| `delete_trusted_proxies` | **destructive** — remove trusted-proxy config |

### Route lifecycle

| Tool | Description |
|------|-------------|
| `update_route` | Replace a route in-place by index |
| `move_route` | Move a route to a different position |
| `duplicate_route` | Copy a route to a new position |
| `delete_route` | **destructive** — delete a route by index |
| `delete_route_by_host` | **destructive** — delete all routes for a hostname |

### TLS & certificates

| Tool | Description |
|------|-------------|
| `get_certificates` | TLS automation policies (domains, ACME issuers, CAs) |
| `list_tls_policies` | List TLS automation policies |
| `get_tls_policy` | Get one TLS policy by index |
| `add_tls_policy` | Add a TLS automation policy |
| `update_tls_policy` | Update a TLS policy by index |
| `delete_tls_policy` | **destructive** — delete a TLS policy by index |
| `set_acme_email` | Set the global ACME registration email |
| `list_loaded_certs` | Certificates in Caddy's live cache |

### PKI (internal CA)

| Tool | Description |
|------|-------------|
| `list_pki_cas` | List Caddy-managed PKI certificate authorities |
| `get_pki_ca` | Details for a PKI CA |
| `get_pki_ca_certificates` | Root/intermediate PEM chain for a PKI CA |
| `renew_pki_ca` | **destructive** — force renewal of a PKI CA's certs |

### Logging

| Tool | Description |
|------|-------------|
| `get_log_config` | Current logging configuration |
| `update_log_config` | **destructive** — replace the logging configuration |

### Config-level & lifecycle

| Tool | Description |
|------|-------------|
| `adapt_config` | Convert a Caddyfile snippet to JSON |
| `reload` | **destructive** — load/replace the running configuration |
| `update_config_path` | **destructive** — PATCH a config node |
| `delete_config_path` | **destructive** — delete a config node |
| `stop_caddy` | **destructive** — gracefully stop the Caddy process |

> ## ⚠️ Destructive tools
>
> Tools flagged **destructive** above (every `delete_*`, `stop_caddy`, `reload`,
> `update_config_path`, `update_log_config`, `renew_pki_ca`, and the access-log
> removers) change or remove live configuration and can take services offline.
> The admin API applies most changes immediately. Take a config backup
> (`get_config`) before bulk edits, and prefer the granular `get_*` tools to
> confirm targets (server name, route index, policy index) before deleting.

## Development

```bash
git clone https://github.com/aaronckj/mcp-caddy
cd mcp-caddy
uv sync --extra dev
uv run pytest -v
```
