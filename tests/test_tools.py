"""Tests for mcp-caddy tools. All HTTP calls are mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# HTTP client layer tests
# ---------------------------------------------------------------------------

async def test_request_uses_caddy_host_env(monkeypatch):
    """_request() builds URL from CADDY_HOST env var."""
    monkeypatch.setenv("CADDY_HOST", "http://10.0.0.31:2019")

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
        "http://10.0.0.31:2019/config/",
    )


async def test_request_uses_default_host(monkeypatch):
    """_request() falls back to http://localhost:2019 when CADDY_HOST is not set."""
    monkeypatch.delenv("CADDY_HOST", raising=False)

    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("mcp_caddy.server.httpx.AsyncClient", return_value=mock_client):
        import mcp_caddy.server as srv
        resp = await srv._request("GET", "/")

    mock_client.request.assert_called_once_with("GET", "http://localhost:2019/")


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

_ROUTES_CONFIG = {
    "apps": {
        "http": {
            "servers": {
                "srv0": {
                    "listen": [":443"],
                    "routes": [
                        {
                            "match": [{"host": ["example.com"]}],
                            "handle": [
                                {
                                    "handler": "reverse_proxy",
                                    "upstreams": [{"dial": "10.0.0.5:8080"}],
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
                                                    "upstreams": [{"dial": "10.0.0.6:9000"}],
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
        }
    }
}


async def test_list_routes_success(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(200, _ROUTES_CONFIG)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_routes()

    routes = result["result"]
    assert isinstance(routes, list)
    assert len(routes) == 2

    first = routes[0]
    assert first["hosts"] == ["example.com"]
    assert first["upstreams"] == ["10.0.0.5:8080"]
    assert first["handler"] == "reverse_proxy"
    assert first["server"] == "srv0"


async def test_list_routes_subroute_upstreams(monkeypatch):
    """Upstreams inside subroute handlers are extracted."""
    async def fake_request(method, path, **kw):
        return make_response(200, _ROUTES_CONFIG)

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.list_routes()

    second = result["result"][1]
    assert second["hosts"] == ["api.example.com"]
    assert second["upstreams"] == ["10.0.0.6:9000"]
    assert second["handler"] == "subroute"


async def test_list_routes_no_http_app(monkeypatch):
    """Returns empty list when config has no http app."""
    async def fake_request(method, path, **kw):
        return make_response(200, {"apps": {}})

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
        {"address": "10.0.0.5:8080", "num_requests": 3, "fails": 0},
        {"address": "10.0.0.6:9000", "num_requests": 0, "fails": 1},
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
    assert upstreams[0]["address"] == "10.0.0.5:8080"
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

_TLS_CONFIG = {
    "apps": {
        "tls": {
            "automation": {
                "policies": [
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
            }
        }
    }
}


async def test_get_certificates_success(monkeypatch):
    async def fake_request(method, path, **kw):
        return make_response(200, _TLS_CONFIG)

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


async def test_get_certificates_no_tls_app(monkeypatch):
    """Returns empty list when config has no tls app."""
    async def fake_request(method, path, **kw):
        return make_response(200, {"apps": {"http": {}}})

    import mcp_caddy.server as srv
    monkeypatch.setattr(srv, "_request", fake_request)
    result = await srv.get_certificates()

    assert result["result"] == []


async def test_get_certificates_no_policies(monkeypatch):
    """Returns empty list when tls app has no automation policies."""
    async def fake_request(method, path, **kw):
        return make_response(200, {"apps": {"tls": {"automation": {}}}})

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
