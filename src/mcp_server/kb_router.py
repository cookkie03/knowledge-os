#!/usr/bin/env python3
"""Single multi-KB MCP server (stdio or HTTP) for KnowledgeOS.

Thin adapter: every tool delegates to `tools.retrieval.Retrieval` and returns the result as
JSON text. Every tool takes an explicit `kb` argument (no default KB, no cross-KB leakage).
Logs go to stderr so the stdio JSON-RPC stream stays clean.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

from tools.retrieval import Retrieval
from tools.system import global_env

logger = logging.getLogger("mcp.kb_router")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# MCP tools return JSON text; the formatting is the adapter's only responsibility.
def _serialize(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


# Same operations as tools.retrieval.Retrieval, with results serialized to JSON text.
class KBRouter(Retrieval):
    def list_kbs(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().list_kbs(*args, **kwargs))

    def ask(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().ask(*args, **kwargs))

    def retrieve_context(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().retrieve_context(*args, **kwargs))

    def find_documents(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().find_documents(*args, **kwargs))

    def job_status(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().job_status(*args, **kwargs))

    def evidence(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().evidence(*args, **kwargs))

    def discover_duplicates(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().discover_duplicates(*args, **kwargs))

    def compare_folders(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().compare_folders(*args, **kwargs))

    def remember(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().remember(*args, **kwargs))

    def search_memory(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().search_memory(*args, **kwargs))

    def deprecate_memory(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().deprecate_memory(*args, **kwargs))

    def inbox_send(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().inbox_send(*args, **kwargs))

    def inbox_consume(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().inbox_consume(*args, **kwargs))

    def inbox_status(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().inbox_status(*args, **kwargs))

    def source_ingest(self, *args: Any, **kwargs: Any) -> str:
        return _serialize(super().source_ingest(*args, **kwargs))


def build_mcp_server(router: KBRouter | None = None) -> FastMCP:
    """Build and configure the FastMCP router instance exposing §6 tools."""
    active_router = router or KBRouter()
    mcp_app = FastMCP("lightrag-router")

    # Register the router methods as MCP tools; each delegates to this router instance.
    @mcp_app.tool()
    def list_kbs() -> str:
        """List all available Knowledge Bases with ports, storage modes, and current operational status."""
        return active_router.list_kbs()

    @mcp_app.tool()
    def ask(
        kb: str,
        question: str,
        strategy: str = "balanced",
        response_format: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Query a Knowledge Base and return the LLM response along with citation references."""
        return active_router.ask(
            kb=kb,
            question=question,
            strategy=strategy,
            context=context,
            response_format=response_format,
        )

    @mcp_app.tool()
    def retrieve_context(
        kb: str,
        question: str,
        strategy: str = "balanced",
        context: dict[str, Any] | None = None,
    ) -> str:
        """Retrieve structured RAG context (entities, relationships, text chunks, source provenance)."""
        return active_router.retrieve_context(
            kb=kb, question=question, strategy=strategy, context=context
        )

    @mcp_app.tool()
    def find_documents(
        kb: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
        source: str | None = None,
        path_prefix: str | None = None,
        active_only: bool = True,
    ) -> str:
        """Query granular document inventory from the catalog with source and path filters."""
        return active_router.find_documents(
            kb=kb,
            status=status,
            page=page,
            page_size=page_size,
            source=source,
            path_prefix=path_prefix,
            active_only=active_only,
        )

    @mcp_app.tool()
    def job_status(kb: str, job_id: str) -> str:
        """Check the progress, stage, counters, and error/retry state of a sync or ingest job."""
        return active_router.job_status(kb=kb, job_id=job_id)

    @mcp_app.tool()
    def evidence(kb: str, doc_id: str, chunk_id: str | None = None) -> str:
        """Inspect extracted content, origin source paths, and metadata for a document or chunk."""
        return active_router.evidence(kb=kb, doc_id=doc_id, chunk_id=chunk_id)

    @mcp_app.tool()
    def discover_duplicates(kb: str, kind: str = "exact", scope: str | None = None) -> str:
        """Find duplicate document clusters across sources (exact SHA256 clusters)."""
        return active_router.discover_duplicates(kb=kb, kind=kind, scope=scope)

    @mcp_app.tool()
    def compare_folders(
        kb: str, left_source: str, right_source: str, mode: str = "novelty"
    ) -> str:
        """Compare knowledge coverage and novelty between two sources in a Knowledge Base."""
        return active_router.compare_folders(
            kb=kb, left_source=left_source, right_source=right_source, mode=mode
        )

    @mcp_app.tool()
    def remember(
        kb: str,
        content: str,
        memory_type: str = "fact",
        title: str = "",
        tags: list[str] | None = None,
        supersedes_id: str | None = None,
        source: str = "agent",
    ) -> str:
        """Store an agent memory in Markdown frontmatter format in <source_dir>/memories/."""
        return active_router.remember(
            kb=kb,
            content=content,
            memory_type=memory_type,
            title=title,
            tags=tags,
            supersedes_id=supersedes_id,
            source=source,
        )

    @mcp_app.tool()
    def search_memory(
        kb: str,
        query: str,
        memory_type: str | None = None,
        active_only: bool = True,
        top_k: int = 5,
    ) -> str:
        """Search structured agent memories by query, status, and type."""
        return active_router.search_memory(
            kb=kb,
            query=query,
            memory_type=memory_type,
            active_only=active_only,
            top_k=top_k,
        )

    @mcp_app.tool()
    def deprecate_memory(
        kb: str,
        memory_id: str,
        reason: str = "",
        superseded_by: str | None = None,
    ) -> str:
        """Mark an agent memory as superseded or deprecated."""
        return active_router.deprecate_memory(
            kb=kb,
            memory_id=memory_id,
            reason=reason,
            superseded_by=superseded_by,
        )

    @mcp_app.tool()
    def inbox_send(
        kb: str,
        items: list[dict[str, Any]],
        source_path: str | None = None,
    ) -> str:
        """Queue file contents or a local path for asynchronous inbox processing."""
        return active_router.inbox_send(kb=kb, items=items, source_path=source_path)

    @mcp_app.tool()
    def inbox_consume(kb: str, job_id: str | None = None) -> str:
        """Start processing pending inbox batches in the background."""
        return active_router.inbox_consume(kb=kb, job_id=job_id)

    @mcp_app.tool()
    def inbox_status(kb: str) -> str:
        """Return asynchronous inbox queue status."""
        return active_router.inbox_status(kb=kb)

    @mcp_app.tool()
    def source_ingest(kb: str, sources: list[str] | None = None, force: bool = False) -> str:
        """Start background ingestion for configured external source paths."""
        return active_router.source_ingest(kb=kb, sources=sources, force=force)

    return mcp_app


# MCP authentication middleware for remote HTTP endpoints.
# Invariant: the token is accepted only in headers (Bearer, Basic, X-API-Key, X-MCP-Token),
# never in the URL, because URLs end up in logs, history and proxies.
class MCPAuthMiddleware:
    """ASGI middleware that enforces token-based authentication across multiple transport schemes."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token.strip()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        # Always allow CORS preflight and health checks
        if method == "OPTIONS" or path == "/health":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="ignore").strip()
        api_key_header = headers.get(b"x-api-key", b"").decode("utf-8", errors="ignore").strip()
        mcp_token_header = headers.get(b"x-mcp-token", b"").decode("utf-8", errors="ignore").strip()

        authorized = False
        # 1. Bearer Token (Claude Web, ChatGPT Actions, Grok, Gemini, curl)
        if auth_header.startswith("Bearer "):
            token_candidate = auth_header[7:].strip()
            if token_candidate == self.token:
                authorized = True

        # 2. HTTP Basic Auth (browsers, web connectors)
        elif auth_header.startswith("Basic "):
            try:
                import base64
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8", errors="ignore")
                user, _, password = decoded.partition(":")
                if password == self.token or user == self.token:
                    authorized = True
            except Exception:
                pass

        # 3. Custom Headers: X-API-Key or X-MCP-Token
        if not authorized:
            if api_key_header == self.token or mcp_token_header == self.token:
                authorized = True

        if authorized:
            await self.app(scope, receive, send)
            return

        # Deny unauthorized access with JSON-RPC error payload and standard 401
        error_body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32001,
                "message": "Unauthorized: valid MCP password or token required",
            },
        }).encode("utf-8")

        response_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(error_body)).encode("ascii")),
            (b"www-authenticate", b'Bearer realm="MCP", Basic realm="MCP"'),
        ]
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": response_headers,
        })
        await send({
            "type": "http.response.body",
            "body": error_body,
        })


# Default module-level router and FastMCP application used by `main`.
router = KBRouter()
mcp = build_mcp_server(router)


def main() -> None:
    """CLI entry point for running the FastMCP server."""
    parser = argparse.ArgumentParser(description="LightRAG-KB Multi-KB MCP Router")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http", "sse"),
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for network transports")
    parser.add_argument("--port", type=int, default=8743, help="Bind port for network transports")
    parser.add_argument("--path", default="/mcp", help="Endpoint path for network transports")
    parser.add_argument(
        "--auth-token",
        default=os.getenv("MCP_AUTH_TOKEN"),
        help="Optional password/token to protect HTTP/SSE endpoints (default: env MCP_AUTH_TOKEN)",
    )
    args, _ = parser.parse_known_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        middleware = []
        token = (args.auth_token or os.getenv("MCP_AUTH_TOKEN") or global_env().get("MCP_AUTH_TOKEN") or "").strip()
        if token:
            from starlette.middleware import Middleware
            middleware.append(Middleware(MCPAuthMiddleware, token=token))
            logger.info("MCP endpoint protected with authentication token.")

        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            path=args.path,
            middleware=middleware,
        )


if __name__ == "__main__":
    main()