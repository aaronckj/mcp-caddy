"""mcp-caddy: Caddy web server management MCP server."""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("caddy")

_DEFAULT_HOST = "http://localhost:2019"
_DEFAULT_TIMEOUT = 30.0


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
