"""Minimal stdio MCP server used by the test suite for hermetic MCP coverage.

Run over stdio with ``python tests/fixtures/mcp_echo_server.py``. Exposes a
single ``echo_text`` tool so tests can exercise the full stdio MCP path
(spawn subprocess → list tools → invoke tool) without any network access.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("olano-test-echo")


@mcp.tool()
def echo_text(text: str) -> str:
    """Echo the provided text back, prefixed so tests can assert on it."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
