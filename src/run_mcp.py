#!/usr/bin/env python3
"""Entry point of the single multi-KB MCP server (stdio or HTTP); the script directory (src/) is on the import path."""

from mcp_server.kb_router import main

if __name__ == "__main__":
    main()
