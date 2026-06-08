"""Tests for mcp-caddy tools. All HTTP calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# HTTP client layer tests
# ---------------------------------------------------------------------------

async def test_request_uses_caddy_host_env(monkeypatch):
    """_request() builds URL from CADDY_HOST env var (no creds -> auth=None)."""
    monkeypatch.setenv("CADDY_HOST", "http://caddy.example:2019")
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    monkeypatch.delenv("CADDY_USERNAME", raising=False)
    monkeypatch.delenv("CADDY_PASSWORD", raising=False)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("mcp_caddy.server.httpx.AsyncClient", return_value=mock_client):
        import mcp_caddy.server as srv
        resp = await srv._request("GET", "/config/")

    assert resp.status_code == 200
    mock_client.request.assert_called_once_with(
        "GET",
        "http://caddy.example:2019/config/",
        auth=None,
    )


async def test_request_uses_default_host(monkeypatch):
    """_request() falls back to http://localhost:2019 when CADDY_HOST is not set."""
    monkeypatch.delenv("CADDY_HOST", raising=False)
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    monkeypatch.delenv("CADDY_USERNAME", raising=False)
    monkeypatch.delenv("CADDY_PASSWORD", raising=False)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("mcp_caddy.server.httpx.AsyncClient", return_value=mock_client):
        import mcp_caddy.server as srv
        resp = await srv._request("GET", "/")

    mock_client.request.assert_called_once_with(
        "GET", "http://localhost:2019/", auth=None
    )


async def test_request_uses_basic_auth(monkeypatch):
    """_request() passes Basic auth when CADDY_USERNAME and CADDY_PASSWORD are set."""
    monkeypatch.setenv("CADDY_HOST", "http://caddy.example:2019")
    monkeypatch.delenv("VAULT_PROXY_URL", raising=False)
    monkeypatch.setenv("CADDY_USERNAME", "admin")
    monkeypatch.setenv("CADDY_PASSWORD", "secret")

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("mcp_caddy.server.httpx.AsyncClient", return_value=mock_client):
        import mcp_caddy.server as srv
        await srv._request("GET", "/")

    mock_client.request.assert_called_once_with(
        "GET", "http://caddy.example:2019/", auth=("admin", "secret")
    )


# ---------------------------------------------------------------------------
# Tool tests — helpers
# ---------------------------------------------------------------------------

def make_response(status: int, data) -> httpx.Response:
    """Build a real httpx.Response with JSON body (no live HTTP needed)."""
    import json
    mock_req = MagicMock()
    resp = httpx.Response(
        status,
        content=json.dumps(data).encode(),
        headers={"content-type": "application/json"},
    )
    resp._request = mock_req
    return resp


# ---------------------------------------------------------------------------
# server_info
# ---------------------------------------------------------------------------

async def test_server_info_success(monkeypatch):
    payload = {"version": "v2.7.6", "modules": ["admin", "http", "tls"]}

    async def fake_request(method, path, **kw):
        assert method == "GET"
        assert path == "/"
        return make_response(200, payload)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.server_info()

    assert result["result"]["version"] == "v2.7.6"
    assert "modules" in result["result"]


async def test_server_info_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.server_info()

    assert "error" in result
    assert result["tool"] == "server_info"


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

async def test_get_config_success(monkeypatch):
    payload = {
        "admin": {"listen": "localhost:2019"},
        "apps": {"http": {"servers": {}}, "tls": {}},
    }

    async def fake_request(method, path, **kw):
        assert method == "GET"
        assert path == "/config/"
        return make_response(200, payload)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_config()

    assert "admin" in result["result"]
    assert "apps" in result["result"]


async def test_get_config_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_config()

    assert "error" in result
    assert result["tool"] == "get_config"


# ---------------------------------------------------------------------------
# list_routes
# ---------------------------------------------------------------------------

# list_routes queries GET /config/apps/http/servers, which returns the
# servers map directly (not the full top-level config).
_SERVERS_CONFIG = {
    "srv0": {
        "listen": [":443"],
        "routes": [
            {
                "match": [{"host": ["example.com"]}],
                "handle": [
                    {
                        "handler": "reverse_proxy",
                        "upstreams": [{"dial": "localhost:8080"}],
                    }
                ],
            },
            {
                "match": [{"host": ["api.example.com"]}],
                "handle": [
                    {
                        "handler": "subroute",
                        "routes": [
                            {
                                "handle": [
                                    {
                                        "handler": "reverse_proxy",
                                        "upstreams": [{"dial": "localhost:9000"}],
                                    }
                                ]
                            }
                        ],
                    }
                ],
            },
        ],
    }
}


async def test_list_routes_success(monkeypatch):
    async def fake_request(method, path, **kw):
        assert path == "/config/apps/http/servers"
        return make_response(200, _SERVERS_CONFIG)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_routes()

    routes = result["result"]
    assert isinstance(routes, list)
    assert len(routes) == 2

    first = routes[0]
    assert first["hosts"] == ["example.com"]
    assert first["upstreams"] == ["localhost:8080"]
    assert first["handler"] == "reverse_proxy"
    assert first["server"] == "srv0"


async def test_list_routes_subroute_upstreams(monkeypatch):
    """Upstreams inside subroute handlers are extracted."""
    async def fake_request(method, path, **kw):
        return make_response(200, _SERVERS_CONFIG)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_routes()

    second = result["result"][1]
    assert second["hosts"] == ["api.example.com"]
    assert second["upstreams"] == ["localhost:9000"]
    assert second["handler"] == "subroute"


async def test_list_routes_no_servers(monkeypatch):
    """Returns empty list when there are no HTTP servers configured."""
    async def fake_request(method, path, **kw):
        return make_response(200, {})

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_routes()

    assert result["result"] == []


async def test_list_routes_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_routes()

    assert "error" in result
    assert result["tool"] == "list_routes"


# ---------------------------------------------------------------------------
# list_upstreams
# ---------------------------------------------------------------------------

async def test_list_upstreams_success(monkeypatch):
    payload = [
        {"address": "localhost:8080", "num_requests": 3, "fails": 0},
        {"address": "localhost:9000", "num_requests": 0, "fails": 1},
    ]

    async def fake_request(method, path, **kw):
        assert method == "GET"
        assert path == "/reverse_proxy/upstreams"
        return make_response(200, payload)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_upstreams()

    upstreams = result["result"]
    assert isinstance(upstreams, list)
    assert len(upstreams) == 2
    assert upstreams[0]["address"] == "localhost:8080"
    assert upstreams[1]["fails"] == 1


async def test_list_upstreams_empty(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(200, [])

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_upstreams()

    assert result["result"] == []


async def test_list_upstreams_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_upstreams()

    assert "error" in result
    assert result["tool"] == "list_upstreams"


# ---------------------------------------------------------------------------
# get_certificates
# ---------------------------------------------------------------------------

# get_certificates queries GET /config/apps/tls/automation/policies, which
# returns the policies array directly (or 404 when the path is absent).
_TLS_POLICIES = [
    {
        "subjects": ["example.com", "www.example.com"],
        "issuers": [
            {
                "module": "acme",
                "ca": "https://acme-v02.api.letsencrypt.org/directory",
                "email": "admin@example.com",
            }
        ],
    },
    {
        "subjects": ["internal.example.com"],
        "issuers": [{"module": "acme", "ca": "https://acme.zerossl.com/v2/DV90"}],
    },
]


async def test_get_certificates_success(monkeypatch):
    async def fake_request(method, path, **kw):
        assert path == "/config/apps/tls/automation/policies"
        return make_response(200, _TLS_POLICIES)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_certificates()

    certs = result["result"]
    assert isinstance(certs, list)
    assert len(certs) == 2
    assert certs[0]["subjects"] == ["example.com", "www.example.com"]
    assert certs[0]["issuers"][0]["module"] == "acme"
    assert certs[0]["issuers"][0]["ca"] == "https://acme-v02.api.letsencrypt.org/directory"
    assert certs[0]["issuers"][0]["email"] == "admin@example.com"


async def test_get_certificates_no_tls_policies(monkeypatch):
    """Returns empty list when the policies path is absent (404)."""
    async def fake_request(method, path, **kw):
        return make_response(404, {"error": "not found"})

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_certificates()

    assert result["result"] == []


async def test_get_certificates_empty_policies(monkeypatch):
    """Returns empty list when automation has no policies (null body)."""
    async def fake_request(method, path, **kw):
        return make_response(200, None)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_certificates()

    assert result["result"] == []


async def test_get_certificates_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_certificates()

    assert "error" in result
    assert result["tool"] == "get_certificates"


# ---------------------------------------------------------------------------
# adapt_config
# ---------------------------------------------------------------------------

async def test_adapt_config_success(monkeypatch):
    caddyfile = "example.com { reverse_proxy localhost:8080 }"
    adapted = {"apps": {"http": {"servers": {"srv0": {"routes": []}}}}}

    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/adapt"
        assert kw.get("params") == {"adapter": "caddyfile"}
        assert kw.get("content") == caddyfile.encode()
        assert kw.get("headers", {}).get("Content-Type") == "text/caddyfile"
        return make_response(200, adapted)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.adapt_config(caddyfile=caddyfile)

    assert "apps" in result["result"]


async def test_adapt_config_syntax_error(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(400, {"error": "Caddyfile syntax error"})

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.adapt_config(caddyfile="invalid {{{")

    assert "error" in result
    assert result["tool"] == "adapt_config"


async def test_adapt_config_network_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.adapt_config(caddyfile="example.com {}")

    assert "error" in result
    assert result["tool"] == "adapt_config"


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------

async def test_reload_json_string(monkeypatch):
    """reload() with a JSON string POSTs directly without file I/O."""
    config = {"apps": {}}
    config_str = '{"apps": {}}'

    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/load"
        assert kw.get("json") == config
        return make_response(200, {})

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.reload(source=config_str)

    assert result["result"]["reloaded"] is True


async def test_reload_file_path(monkeypatch, tmp_path):
    """reload() with a file path reads the file and POSTs its contents."""
    import json
    config = {"apps": {"http": {}}}
    config_file = tmp_path / "caddy.json"
    config_file.write_text(json.dumps(config))

    async def fake_request(method, path, **kw):
        assert method == "POST"
        assert path == "/load"
        assert kw.get("json") == config
        return make_response(200, {})

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.reload(source=str(config_file))

    assert result["result"]["reloaded"] is True


async def test_reload_invalid_json(monkeypatch):
    """reload() returns error dict when source is not valid JSON and not a valid file."""
    import mcp_caddy.server as srv
    result = await srv.reload(source="not json at all")

    assert "error" in result
    assert result["tool"] == "reload"


async def test_reload_network_error(monkeypatch):
    async def fake_request(method, path, **kw):
        raise httpx.ConnectError("Connection refused")

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.reload(source='{"apps": {}}')

    assert "error" in result
    assert result["tool"] == "reload"
