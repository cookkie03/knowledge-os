from __future__ import annotations

import base64
import sys
from pathlib import Path
import pytest
from starlette.testclient import TestClient
from starlette.middleware import Middleware
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcp"))

from kb_router import MCPAuthMiddleware


def test_mcp_auth_middleware_schemes() -> None:
    token = "test-secret-key-123"
    test_mcp = FastMCP("test-auth")

    @test_mcp.tool()
    def ping() -> str:
        return "pong"

    app = test_mcp.http_app(
        transport="streamable-http",
        middleware=[Middleware(MCPAuthMiddleware, token=token)],
    )

    with TestClient(app) as client:
        # 1. Without credentials -> 401 Unauthorized
        res_unauth = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert res_unauth.status_code == 401
        assert "www-authenticate" in res_unauth.headers
        assert "Unauthorized" in res_unauth.text

        # 2. Invalid Bearer token -> 401 Unauthorized
        res_wrong = client.post("/mcp", headers={"Authorization": "Bearer wrong-token"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert res_wrong.status_code == 401

        # 3. Valid Bearer Token (Claude Web / ChatGPT Actions / Grok / Gemini) -> allowed through middleware (passes auth gate)
        res_bearer = client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert res_bearer.status_code != 401

        # 4. Valid HTTP Basic Auth -> passes auth gate
        basic_b64 = base64.b64encode(f"user:{token}".encode("utf-8")).decode("utf-8")
        res_basic = client.post(
            "/mcp",
            headers={"Authorization": f"Basic {basic_b64}"},
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        )
        assert res_basic.status_code != 401

        # 5. Valid X-API-Key header -> passes auth gate
        res_header = client.post(
            "/mcp",
            headers={"X-API-Key": token},
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        )
        assert res_header.status_code != 401

        # 6. Token in the URL is rejected: URLs leak into logs and proxies
        res_query = client.post(
            f"/mcp?token={token}",
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
        )
        assert res_query.status_code == 401
