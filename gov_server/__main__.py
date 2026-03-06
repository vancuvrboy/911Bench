"""Module entrypoint for running governance MCP server."""

from .mcp_server import run_server


if __name__ == "__main__":
    raise SystemExit(run_server())

