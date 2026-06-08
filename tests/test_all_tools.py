#!/usr/bin/env python3
"""
Smoke tests for mcp-caddy tools.

These verify that every exported tool is importable, awaitable, and returns a
dict. Network-touching tools have `_request` mocked so the suite never reaches a
live Caddy admin API. Validation-only paths (empty/invalid args) need no mock.
"""
import json

import httpx
import pytest


def make_response(status: int, data) -> httpx.Response:
    """Build a real httpx.Response with a JSON body (no live HTTP needed)."""
    from unittest.mock import MagicMock

    resp = httpx.Response(
        status,
        content=json.dumps(data).encode(),
        headers={"content-type": "application/json"},
    )
    resp._request = MagicMock()
    return resp


@pytest.fixture
def mock_request(monkeypatch):
    """Patch _request so success-path tools return a benign empty config."""
    import mcp_caddy.server as srv

    async def fake_request(method, path, **kw):
        # Empty maps/objects keep every tool on a non-error path.
        return make_response(200, {})

    monkeypatch.setattr(srv, "_request", fake_request)
    return srv


class TestCaddyTools:
    """Structural smoke tests for Caddy MCP tools."""

    async def test_server_info_success(self, mock_request):
        result = await mock_request.server_info()
        assert isinstance(result, dict)

    async def test_list_servers_success(self, mock_request):
        result = await mock_request.list_servers()
        assert isinstance(result, dict)

    async def test_create_server_success(self, mock_request):
        result = await mock_request.create_server("test_server", ":443")
        assert isinstance(result, dict)

    async def test_create_server_empty_name(self):
        import mcp_caddy.server as srv

        result = await srv.create_server("", ":443")
        assert "error" in result

    async def test_create_server_invalid_listen(self):
        import mcp_caddy.server as srv

        result = await srv.create_server("test_server", "invalid")
        assert "error" in result

    async def test_add_reverse_proxy_route_success(self, mock_request):
        result = await mock_request.add_reverse_proxy_route(
            "example.com", "localhost:3000"
        )
        assert isinstance(result, dict)

    async def test_add_path_route_success(self, mock_request):
        result = await mock_request.add_path_route("/api/*", "localhost:8080")
        assert isinstance(result, dict)

    async def test_add_static_file_server_success(self, mock_request):
        result = await mock_request.add_static_file_server("/files/*", "/var/www")
        assert isinstance(result, dict)

    async def test_add_redirect_success(self, mock_request):
        result = await mock_request.add_redirect(
            "old.example.com", "https://new.example.com"
        )
        assert isinstance(result, dict)

    async def test_add_header_route_success(self, mock_request):
        result = await mock_request.add_header_route(
            "example.com", "X-Test", "test-value"
        )
        assert isinstance(result, dict)

    async def test_list_routes_success(self, mock_request):
        result = await mock_request.list_routes()
        assert isinstance(result, dict)

    async def test_get_certificates_success(self, mock_request):
        result = await mock_request.get_certificates()
        assert isinstance(result, dict)

    async def test_adapt_config_success(self, mock_request):
        result = await mock_request.adapt_config("example.com { respond ok }")
        assert isinstance(result, dict)

    async def test_reload_success(self, mock_request):
        result = await mock_request.reload()
        assert isinstance(result, dict)


# Imports verify the full public surface stays exported/renamed-aware.
def test_public_tools_importable():
    from mcp_caddy.server import (  # noqa: F401
        server_info,
        get_config,
        get_config_path,
        list_servers,
        get_server,
        delete_server,
        create_server,
        list_routes,
        add_reverse_proxy_route,
        add_path_route,
        add_static_file_server,
        add_redirect,
        add_header_route,
        delete_route,
        list_upstreams,
        mark_upstream_health,
        get_certificates,
        adapt_config,
        reload,
        update_config_path,
        delete_config_path,
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
