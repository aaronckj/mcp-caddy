# mcp-caddy

MCP server for [Caddy](https://caddyserver.com/) web server management. Exposes 7 tools for inspecting routes, upstream health, certificates, and reloading configuration via the Caddy admin API.

## Quick Start

**With uvx (recommended):**
```bash
CADDY_HOST=http://10.0.0.31:2019 uvx mcp-caddy
```

**With Docker:**
```bash
docker run -i \
  -e CADDY_HOST=http://10.0.0.31:2019 \
  ghcr.io/aaronckj/mcp-caddy:latest
```

**Add to Claude Code:**
```bash
claude mcp add caddy -s user -e CADDY_HOST=http://10.0.0.31:2019 -- uvx mcp-caddy
```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CADDY_HOST` | No | `http://localhost:2019` | Caddy admin API URL |
| `CADDY_TIMEOUT` | No | `30` | HTTP timeout in seconds |

## Finding Your CADDY_HOST

The Caddy admin API binds to `localhost:2019` by default. Depending on your setup:

1. **MCP server on same host as Caddy** → use the default `http://localhost:2019`
2. **Caddy in Docker, MCP server on the same Docker host** → add `ports: ["127.0.0.1:2019:2019"]` to your Caddy compose service, then use `http://localhost:2019`
3. **Remote Caddy host** → SSH tunnel: `ssh -L 2019:localhost:2019 user@caddy-host`, then use `http://localhost:2019`
4. **Caddy exposes admin via reverse proxy** → set `CADDY_HOST=https://caddy-admin.example.com`

The Caddy admin API has no authentication by default. If you expose it beyond localhost, add Caddy basic auth to that route.

## Tools

| Tool | Description |
|------|-------------|
| `server_info` | Caddy version and loaded modules |
| `get_config` | Full configuration as JSON |
| `list_routes` | All virtual hosts with upstream targets |
| `list_upstreams` | Upstream proxy health status |
| `get_certificates` | TLS automation policies and ACME issuers |
| `adapt_config` | Convert a Caddyfile snippet to JSON |
| `reload` | Reload config from a JSON string or file path |

## Development

```bash
git clone https://github.com/aaronckj/mcp-caddy
cd mcp-caddy
uv sync --extra dev
uv run pytest -v
```
