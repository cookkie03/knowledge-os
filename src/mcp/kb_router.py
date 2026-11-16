#!/usr/bin/env python3
"""Multi-KB MCP Router for LightRAG-KB.

Implements the single stdio/remote FastMCP router according to §6 and §11 of:
`LightRAG-KB · Piano monolitico production-grade (Debian + MCP + one-shot sync)`

Core principles:
- Explicit `kb` parameter on all KB tools (no default fallback or cross-KB leakage).
- Reuses existing endpoints while preserving metadata:
  - `list_kbs()`: names, IDs, status, degraded detection.
  - `ask(kb, question, strategy, context)`: preserves response AND references.
  - `retrieve_context(kb, question, strategy, context)`: original chunks/entities/relations/source IDs.
  - `find_documents(kb, status, page, page_size, source?)`: granular inventory via Catalog.
  - `job_status(kb, job_id)`: progress, stage, counters, retry/review flags.
  - `evidence(kb, doc_id, chunk_id?)`: extracted content, original occurrences, metadata.
  - `discover_duplicates(kb, kind, scope?)`: exact SHA clusters supported; unimplemented kinds return FEATURE_NOT_ENABLED.
  - `compare_folders(kb, left, right, mode)`: returns FEATURE_NOT_ENABLED (no hallucinated results).
- Clean JSON-RPC over stdio (all logs/warnings to stderr, never stdout).
- Structured errors for backend offline and missing KBs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

import requests

try:
    from fastmcp import FastMCP
except ImportError:
    from mcp.server.fastmcp import FastMCP  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src" / "cli") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "cli"))

from kb_registry import find_kb, global_env, load_registry, storage_mode  # noqa: E402
from mcp.memories import (  # noqa: E402
    deprecate_memory_by_id,
    query_memories,
    remember_memory,
    compact_episodic_memories,
    find_conflicting_decisions,
    run_dreaming_cycle,
    serialize_memory_frontmatter,
    write_memory_record,
)
from storage.catalog import Catalog, DEFAULT_KB_ROOT  # noqa: E402
from storage.inbox import get_inbox_status, queue_inbox_send, spawn_detached_job

logger = logging.getLogger("mcp.kb_router")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

STRATEGY_TO_MODE: dict[str, str] = {
    "balanced": "mix",
    "entity_focus": "local",
    "theme_focus": "global",
    "hybrid": "hybrid",
    "vector_only": "naive",
    "llm_only": "bypass",
    # Direct mode pass-through
    "mix": "mix",
    "local": "local",
    "global": "global",
    "naive": "naive",
    "bypass": "bypass",
}

ALLOWED_CONTEXT_FIELDS = {
    "top_k",
    "chunk_top_k",
    "max_entity_tokens",
    "max_relation_tokens",
    "max_total_tokens",
    "rerank",
    "hl_keywords",
    "ll_keywords",
    "prompt_override",
}


def _serialize(data: Any) -> str:
    """Serialize data to formatted JSON string."""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _normalize_strategy(strategy: str | None) -> str:
    """Normalize user strategy string to LightRAG mode.

    Invariant: every allowed strategy maps to exactly one LightRAG mode; unknown
    values raise ValueError. The STRATEGY_TO_MODE dict is the single source of
    truth — callers must not hard‑code mode strings.
    """
    if strategy is None:
        return "mix"
    mode = STRATEGY_TO_MODE.get(strategy.strip().lower())
    if mode is None:
        allowed = ", ".join(sorted(set(STRATEGY_TO_MODE.keys())))
        raise ValueError(f"Invalid strategy: '{strategy}'. Allowed values: {allowed}.")
    return mode


def _normalize_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize context parameters for LightRAG query payloads.

    Invariant: only the fields listed in ALLOWED_CONTEXT_FIELDS are permitted;
    any unknown field raises ValueError. The payload dict is built with None‑valued
    entries filtered out, so the downstream RPC never sends meaningless keys.
    """
    if context is None:
        return {}
    unknown = sorted(set(context) - ALLOWED_CONTEXT_FIELDS)
    if unknown:
        allowed = ", ".join(sorted(ALLOWED_CONTEXT_FIELDS))
        raise ValueError(
            f"Unrecognized context parameters: {', '.join(unknown)}. Allowed fields: {allowed}."
        )
    payload = {
        "top_k": context.get("top_k"),
        "chunk_top_k": context.get("chunk_top_k"),
        "max_entity_tokens": context.get("max_entity_tokens"),
        "max_relation_tokens": context.get("max_relation_tokens"),
        "max_total_tokens": context.get("max_total_tokens"),
        "enable_rerank": context.get("rerank"),
        "hl_keywords": context.get("hl_keywords"),
        "ll_keywords": context.get("ll_keywords"),
        "user_prompt": context.get("prompt_override"),
    }
    return {k: v for k, v in payload.items() if v is not None}


class KBRouter:
    """Route KB requests to the registry, LightRAG backend and JSON catalog."""

    # Keep injected registry, storage and HTTP dependencies for KB-scoped requests.
    def __init__(
        self,
        registry: dict[str, Any] | Callable[[], dict[str, Any]] | None = None,
        kb_root: Path | str | None = None,
        env: dict[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._registry_source = registry
        self.kb_root = Path(kb_root).resolve() if kb_root is not None else None
        self.env = env
        self.session = session or requests.Session()

    def get_registry(self) -> dict[str, Any]:
        """Fetch current registry mapping."""
        if callable(self._registry_source):
            return self._registry_source()
        if isinstance(self._registry_source, dict):
            return self._registry_source
        return load_registry(self.env)

    def get_kb(self, kb_name: str) -> dict[str, Any] | None:
        """Look up a specific KB in the registry."""
        return find_kb(self.get_registry(), kb_name)

    def get_catalog(self, kb_name: str) -> Catalog:
        """Open the KB's JSON catalog without silently bypassing legacy data."""
        if self.kb_root is not None:
            return Catalog.open_for_kb(kb_name, kb_root=self.kb_root)
        kb = self.get_kb(kb_name)
        if kb and kb.get("data_dir"):
            directory = Path(kb["data_dir"])
            return Catalog.open_for_kb(directory.name, kb_root=directory.parent)
        return Catalog.open_for_kb(kb_name, kb_root=DEFAULT_KB_ROOT)

    def check_server_health(self, port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
        """Check whether the LightRAG HTTP server is healthy on localhost:port."""
        try:
            resp = self.session.get(f"http://{host}:{port}/health", timeout=timeout)
            return resp.ok
        except Exception:
            return False

    def check_kb_degraded(self, kb_name: str) -> tuple[bool, str | None]:
        """Check if KB has unresolved job failures or document extraction errors."""
        try:
            with self.get_catalog(kb_name) as cat:
                # Check recent jobs for failure or review markers
                recent_jobs = cat.list_jobs(limit=25)
                for j in recent_jobs:
                    if j["stage"] in ("FAILED_RETRYABLE", "FAILED_FINAL", "NEEDS_REVIEW"):
                        reason = f"Job '{j['job_id']}' in stato {j['stage']}"
                        if j["error_summary"]:
                            reason += f": {j['error_summary']}"
                        return True, reason

                # Check if any document failed indexing/extraction
                failed_docs = cat.find_documents(status="FAILED", page=1, page_size=1)
                if failed_docs.get("total", 0) > 0:
                    return True, f"{failed_docs['total']} documenti in stato FAILED"
        except Exception as exc:
            logger.debug("Catalog check exception for KB %s: %s", kb_name, exc)
        return False, None

    def _post_backend(
        self, kb_name: str, path: str, payload: dict[str, Any], timeout: int = 300
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Send a POST request to the KB's LightRAG backend server.

        Returns (data, error_dict).
        """
        kb = self.get_kb(kb_name)
        if not kb:
            return None, {
                "error": "KB_NOT_FOUND",
                "kb": kb_name,
                "message": f"KB '{kb_name}' not found in registry. No fallback or cross-KB allowed.",
            }

        port = int(kb["port"])
        if not self.check_server_health(port):
            return None, {
                "error": "BACKEND_OFFLINE",
                "kb": kb_name,
                "port": port,
                "message": f"The server for KB '{kb_name}' is not running on :{port}. Start it with `ragcli start {kb_name}`.",
            }

        url = f"http://127.0.0.1:{port}{path}"
        try:
            resp = self.session.post(url, json=payload, timeout=timeout)
            if resp.status_code >= 400:
                return None, {
                    "error": "BACKEND_ERROR",
                    "kb": kb_name,
                    "port": port,
                    "status_code": resp.status_code,
                    "message": resp.text or f"HTTP error {resp.status_code} from LightRAG server.",
                }
            try:
                return resp.json(), None
            except Exception:
                return {"response": resp.text}, None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            return None, {
                "error": "BACKEND_OFFLINE",
                "kb": kb_name,
                "port": port,
                "message": f"Connection failed to backend for KB '{kb_name}' on :{port} ({exc}).",
            }
        except Exception as exc:
            return None, {
                "error": "REQUEST_FAILED",
                "kb": kb_name,
                "message": str(exc),
            }

    # -------------------------------------------------------------------------
    # Core Tools Implementation (§6)
    # -------------------------------------------------------------------------

    def list_kbs(self) -> str:
        """List all registered Knowledge Bases with IDs, ports, modes, and operational state."""
        reg = self.get_registry()
        kbs_list = []
        for kb in reg.get("kbs", []):
            name = kb["name"]
            port = int(kb["port"])
            is_up = self.check_server_health(port)
            is_degraded, degraded_reason = self.check_kb_degraded(name)

            if is_degraded:
                status = "DEGRADED" if is_up else "OFFLINE (DEGRADED)"
            else:
                status = "READY" if is_up else "OFFLINE"

            # Query counts from Catalog if available
            sources_count = 0
            docs_count = 0
            try:
                with self.get_catalog(name) as cat:
                    sources_count = len(cat.list_sources(status="active"))
                    docs_summary = cat.find_documents(page=1, page_size=1)
                    docs_count = docs_summary.get("total", 0)
            except Exception:
                pass

            info: dict[str, Any] = {
                "name": name,
                "port": port,
                "status": status,
                "mode": storage_mode(kb) if "source_dir" in kb and "data_dir" in kb else "repo",
                "source_dir": kb.get("source_dir", ""),
                "sources_count": sources_count,
                "documents_count": docs_count,
                "degraded": is_degraded,
            }
            if degraded_reason:
                info["degraded_reason"] = degraded_reason
            kbs_list.append(info)

        return _serialize({"kbs": kbs_list, "total": len(kbs_list)})

    def ask(
        self,
        kb: str,
        question: str,
        strategy: str = "balanced",
        context: dict[str, Any] | None = None,
        response_format: str | None = None,
    ) -> str:
        """Query a Knowledge Base, returning both LLM answer and provenance references (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        try:
            mode = _normalize_strategy(strategy)
        except ValueError as exc:
            return _serialize({
                "error": "INVALID_STRATEGY",
                "kb": kb,
                "strategy": strategy,
                "message": str(exc),
            })

        try:
            normalized_ctx = _normalize_context(context)
        except ValueError as exc:
            return _serialize({
                "error": "INVALID_CONTEXT",
                "kb": kb,
                "message": str(exc),
            })

        payload: dict[str, Any] = {
            "query": question,
            "mode": mode,
            **normalized_ctx,
        }
        if response_format is not None:
            payload["response_type"] = response_format

        data, err = self._post_backend(kb, "/query", payload)
        if err:
            return _serialize(err)

        # Preserve both response and references (§6: l'attuale ask perde references)
        result: dict[str, Any]
        if isinstance(data, dict):
            result = dict(data)
            if "response" not in result and "data" in result:
                result["response"] = result["data"]
            result.setdefault("references", [])
        else:
            result = {"response": data, "references": []}

        # Explicitly signal if KB is degraded
        is_degraded, degraded_reason = self.check_kb_degraded(kb)
        if is_degraded:
            result["degraded"] = True
            result["warning"] = f"KB '{kb}' è in stato DEGRADED ({degraded_reason})."

        return _serialize(result)

    def retrieve_context(
        self,
        kb: str,
        question: str,
        strategy: str = "balanced",
        context: dict[str, Any] | None = None,
    ) -> str:
        """Retrieve raw context data (original chunks, entities, relations, source IDs) (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        try:
            mode = _normalize_strategy(strategy)
        except ValueError as exc:
            return _serialize({
                "error": "INVALID_STRATEGY",
                "kb": kb,
                "strategy": strategy,
                "message": str(exc),
            })

        try:
            normalized_ctx = _normalize_context(context)
        except ValueError as exc:
            return _serialize({
                "error": "INVALID_CONTEXT",
                "kb": kb,
                "message": str(exc),
            })

        payload = {
            "query": question,
            "mode": mode,
            **normalized_ctx,
        }
        data, err = self._post_backend(kb, "/query/data", payload)
        if err:
            return _serialize(err)

        if isinstance(data, dict):
            is_degraded, degraded_reason = self.check_kb_degraded(kb)
            if is_degraded:
                data["degraded"] = True
                data["warning"] = f"KB '{kb}' è in stato DEGRADED ({degraded_reason})."

        return _serialize(data)

    def find_documents(
        self,
        kb: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
        source: str | None = None,
        path_prefix: str | None = None,
        active_only: bool = True,
    ) -> str:
        """Query granular inventory with source/path filters and pagination using Catalog (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        with self.get_catalog(kb) as cat:
            docs_data = cat.find_documents(
                status=status,
                source_id=source,
                path_prefix=path_prefix,
                active_only=active_only,
                page=page,
                page_size=page_size,
            )

        is_degraded, degraded_reason = self.check_kb_degraded(kb)
        if is_degraded:
            docs_data["degraded"] = True
            docs_data["warning"] = f"KB '{kb}' è in stato DEGRADED ({degraded_reason})."

        return _serialize(docs_data)

    def job_status(self, kb: str, job_id: str) -> str:
        """Check progress and retry/review state for a sync/ingest job (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        with self.get_catalog(kb) as cat:
            job = cat.get_job(job_id)
            if job is None:
                return _serialize({
                    "error": "JOB_NOT_FOUND",
                    "kb": kb,
                    "job_id": job_id,
                    "message": f"Job '{job_id}' not found in KB '{kb}'.",
                })

            result = {
                "job_id": job["job_id"],
                "kb": job["kb_id"],
                "stage": job["stage"],
                "idempotency_key": job["idempotency_key"],
                "source_id": job["source_id"],
                "scan_id": job["scan_id"],
                "progress": job["counters"],
                "warnings": job["warnings"],
                "error_summary": job["error_summary"],
                "retryable": job["stage"] == "FAILED_RETRYABLE",
                "needs_review": job["stage"] == "NEEDS_REVIEW",
                "resume_marker": job["resume_marker"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
            }
            return _serialize(result)

    def evidence(self, kb: str, doc_id: str, chunk_id: str | None = None) -> str:
        """Inspect extracted content, origin path, and metadata for a document or chunk (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        with self.get_catalog(kb) as cat:
            doc = cat.get_document(doc_id)
            if doc is None:
                doc = cat.get_document_by_lightrag_id(doc_id)
            if doc is None:
                return _serialize({
                    "error": "DOCUMENT_NOT_FOUND",
                    "kb": kb,
                    "doc_id": doc_id,
                    "message": f"Document '{doc_id}' not found in catalog for KB '{kb}'.",
                })

            occurrences = cat.resolve_evidence(doc["canonical_doc_id"])

        artifact_path = doc["extracted_artifact_path"]
        content_snippet: str | None = None
        chunk_info: dict[str, Any] | None = None
        metadata: dict[str, Any] = {}

        if artifact_path:
            p = Path(artifact_path)
            if p.is_file():
                metadata["artifact_file"] = str(p)
                metadata["artifact_size"] = p.stat().st_size
                try:
                    raw_text = p.read_text(encoding="utf-8", errors="replace")
                    if p.suffix.lower() == ".json":
                        try:
                            parsed = json.loads(raw_text)
                            if isinstance(parsed, dict):
                                chunks = parsed.get("chunks") or parsed.get("items") or []
                                if isinstance(chunks, list) and chunk_id:
                                    for c in chunks:
                                        if isinstance(c, dict) and (
                                            c.get("id") == chunk_id or c.get("chunk_id") == chunk_id
                                        ):
                                            chunk_info = c
                                            content_snippet = c.get("text") or c.get("content")
                                            break
                                if chunk_info is None and chunks:
                                    metadata["total_chunks"] = len(chunks)
                                if content_snippet is None:
                                    content_snippet = (
                                        parsed.get("text")
                                        or parsed.get("content")
                                        or raw_text[:4000]
                                    )
                                metadata["doc_metadata"] = {
                                    k: v
                                    for k, v in parsed.items()
                                    if k not in ("chunks", "text", "content")
                                }
                        except json.JSONDecodeError:
                            content_snippet = raw_text[:4000]
                    else:
                        # Markdown / text snippet
                        if chunk_id:
                            metadata["requested_chunk_id"] = chunk_id
                        content_snippet = raw_text[:4000]
                except Exception as exc:
                    metadata["read_error"] = str(exc)
            else:
                metadata["artifact_file_missing"] = True

        is_degraded, degraded_reason = self.check_kb_degraded(kb)
        result: dict[str, Any] = {
            "canonical_doc_id": doc["canonical_doc_id"],
            "lightrag_doc_id": doc["lightrag_doc_id"],
            "lightrag_status": doc["lightrag_status"],
            "derived_hash": doc["derived_hash"],
            "extracted_artifact_path": doc["extracted_artifact_path"],
            "occurrences": occurrences,
            "content_snippet": content_snippet,
            "chunk": chunk_info,
            "metadata": metadata,
        }
        if is_degraded:
            result["degraded"] = True
            result["warning"] = f"KB '{kb}' è in stato DEGRADED ({degraded_reason})."

        return _serialize(result)

    def discover_duplicates(self, kb: str, kind: str = "exact", scope: str | None = None) -> str:
        """Find duplicate clusters across sources in the KB (§6, §8 A1)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        kind_norm = (kind or "exact").strip().lower()
        if kind_norm in ("exact", "sha256", "binary"):
            with self.get_catalog(kb) as cat:
                clusters = cat.find_exact_duplicate_clusters(active_only=True)
                if scope:
                    clusters = [
                        cl
                        for cl in clusters
                        if any(occ.get("source_id") == scope for occ in cl.get("occurrences", []))
                    ]
            return _serialize({
                "clusters": clusters,
                "total_clusters": len(clusters),
                "kind": kind,
                "scope": scope,
            })

        # Future advanced duplicate modules (A2-A5)
        return _serialize({
            "error": "FEATURE_NOT_ENABLED",
            "feature": f"discover_duplicates:{kind}",
            "kb": kb,
            "message": (
                f"Duplicate mode '{kind}' is not currently enabled for KB '{kb}'. "
                "Only 'exact' mode (A1 binary SHA256) is active."
            ),
        })

    def compare_folders(
        self, kb: str, left_source: str, right_source: str, mode: str = "novelty"
    ) -> str:
        """Compare knowledge between two sources in a KB (§6, §8 A6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            })

        # Advanced feature A6 not yet enabled
        return _serialize({
            "error": "FEATURE_NOT_ENABLED",
            "feature": "compare_folders",
            "kb": kb,
            "left_source": left_source,
            "right_source": right_source,
            "mode": mode,
            "message": (
                f"Advanced folder comparison (A6 knowledge novelty, mode='{mode}') "
                f"is not currently enabled for KB '{kb}'."
            ),
        })

    def remember(
        self,
        kb: str,
        content: str,
        memory_type: str = "fact",
        title: str = "",
        tags: list[str] | None = None,
        supersedes_id: str | None = None,
        source: str = "agent",
    ) -> str:
        """Store an agent memory in Markdown frontmatter format in <source_dir>/memories/."""
        kb_info = self.get_kb(kb)
        if not kb_info:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry.",
            })

        source_dir = kb_info.get("source_dir")
        try:
            mem = remember_memory(
                source_dir=source_dir,
                content=content,
                memory_type=memory_type,
                title=title,
                tags=tags,
                supersedes_id=supersedes_id,
                source=source,
            )
            return _serialize({
                "status": "ok",
                "kb": kb,
                "memory": mem.to_dict() if hasattr(mem, "to_dict") else mem,
            })
        except Exception as exc:
            return _serialize({
                "error": "REMEMBER_FAILED",
                "kb": kb,
                "message": str(exc),
            })

    def search_memory(
        self,
        kb: str,
        query: str,
        memory_type: str | None = None,
        active_only: bool = True,
        top_k: int = 5,
    ) -> str:
        """Search structured agent memories by query, status, and type."""
        kb_info = self.get_kb(kb)
        if not kb_info:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry.",
            })

        source_dir = kb_info.get("source_dir")
        try:
            results = query_memories(
                source_dir=source_dir,
                query=query,
                memory_type=memory_type,
                active_only=active_only,
                top_k=top_k,
            )
            return _serialize({
                "status": "ok",
                "kb": kb,
                "count": len(results),
                "memories": [m.to_dict() if hasattr(m, "to_dict") else m for m in results],
            })
        except Exception as exc:
            return _serialize({
                "error": "SEARCH_MEMORY_FAILED",
                "kb": kb,
                "message": str(exc),
            })

    def deprecate_memory(
        self,
        kb: str,
        memory_id: str,
        reason: str = "",
        superseded_by: str | None = None,
    ) -> str:
        """Mark an agent memory as superseded or deprecated."""
        kb_info = self.get_kb(kb)
        if not kb_info:
            return _serialize({
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry.",
            })

        source_dir = kb_info.get("source_dir")
        try:
            updated = deprecate_memory_by_id(
                source_dir=source_dir,
                memory_id=memory_id,
                reason=reason,
                superseded_by=superseded_by,
            )
            if not updated:
                return _serialize({
                    "error": "MEMORY_NOT_FOUND",
                    "kb": kb,
                    "memory_id": memory_id,
                    "message": f"Memory '{memory_id}' not found.",
                })
            return _serialize({
                "status": "ok",
                "kb": kb,
                "memory": updated.to_dict() if hasattr(updated, "to_dict") else updated,
            })
        except Exception as exc:
            return _serialize({
                "error": "DEPRECATE_MEMORY_FAILED",
                "kb": kb,
                "message": str(exc),
            })

    def inbox_send(
        self,
        kb: str,
        items: list[dict[str, Any]],
        source_path: str | None = None,
    ) -> str:
        """Queue uploaded files or a local directory for asynchronous inbox processing."""
        try:
            record = self.get_kb(kb)
            if not record:
                return _serialize({"error": "KB_NOT_FOUND", "kb": kb})
            job = queue_inbox_send(record, source_path=source_path, items=items if source_path is None else None)
            return _serialize({"ok": True, "job_id": job["job_id"], "status": "queued", "file_count": job.get("file_count", 0)})
        except Exception as exc:
            return _serialize({"error": "INBOX_SEND_FAILED", "kb": kb, "message": str(exc)})

    def inbox_consume(self, kb: str, job_id: str | None = None) -> str:
        """Start asynchronous processing of pending inbox batches."""
        try:
            record = self.get_kb(kb)
            if not record:
                return _serialize({"error": "KB_NOT_FOUND", "kb": kb})
            engine_python = Path(global_env().get("ENGINE_PYTHON", sys.executable))
            worker_cmd = [str(engine_python), str(ROOT / "src" / "cli" / "ragcli.py"), "_worker_consume", kb]
            if job_id:
                worker_cmd.extend(["--job", job_id])
            log_path = Path(record["data_dir"]) / "inbox.log"
            pid = spawn_detached_job(worker_cmd, record["data_dir"], log_path, env=os.environ.copy())
            return _serialize({"ok": True, "status": "queued", "pid": pid, "job_id": job_id})
        except Exception as exc:
            return _serialize({"error": "INBOX_CONSUME_FAILED", "kb": kb, "message": str(exc)})

    def inbox_status(self, kb: str) -> str:
        """Return pending, processing, processed and failed inbox batch counts."""
        record = self.get_kb(kb)
        if not record:
            return _serialize({"error": "KB_NOT_FOUND", "kb": kb})
        return _serialize(get_inbox_status(record))

    def source_ingest(self, kb: str, sources: list[str] | None = None, force: bool = False) -> str:
        """Start asynchronous incremental ingestion for configured external source paths."""
        try:
            record = self.get_kb(kb)
            if not record:
                return _serialize({"error": "KB_NOT_FOUND", "kb": kb})
            configured = record.get("sources", [])
            selected = sources or configured
            unknown = [str(Path(item).expanduser().resolve()) for item in selected if str(Path(item).expanduser().resolve()) not in configured]
            if unknown:
                return _serialize({"error": "SOURCE_NOT_CONFIGURED", "sources": unknown})
            engine_python = Path(global_env().get("ENGINE_PYTHON", sys.executable))
            pids = []
            for source in selected:
                cmd = [str(engine_python), str(ROOT / "src" / "cli" / "ingest.py"), "--kb", kb, "--source-folder", source]
                if force:
                    cmd.append("--force")
                log_path = Path(record["data_dir"]) / "ingest.log"
                pids.append(spawn_detached_job(cmd, record["data_dir"], log_path, env=os.environ.copy()))
            return _serialize({"ok": True, "status": "queued", "sources": selected, "pids": pids})
        except Exception as exc:
            return _serialize({"error": "SOURCE_INGEST_FAILED", "kb": kb, "message": str(exc)})

    def ingest_url(
        self,
        kb: str,
        url: str,
        subfolder: str = "media",
        auto_index: bool = True,
        custom_title: str | None = None,
    ) -> str:
        """Download media from YouTube, Soundcloud, Instagram, TikTok, etc. via yt-dlp, transcribe with Whisper, and ingest into KB."""
        try:
            from url_importer import import_media_url_to_kb
            result = import_media_url_to_kb(
                kb_name=kb,
                url=url,
                subfolder=subfolder,
                auto_index=auto_index,
                custom_title=custom_title,
            )
            return _serialize(result)
        except Exception as exc:
            return _serialize({
                "error": "MEDIA_IMPORT_FAILED",
                "kb": kb,
                "url": url,
                "message": str(exc),
            })



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

    @mcp_app.tool()
    def ingest_url(
        kb: str,
        url: str,
        subfolder: str = "media",
        auto_index: bool = True,
        custom_title: str | None = None,
    ) -> str:
        """Download media from YouTube, Soundcloud, Instagram, TikTok, etc. via yt-dlp, transcribe with Whisper, and ingest into KB."""
        return active_router.ingest_url(
            kb=kb,
            url=url,
            subfolder=subfolder,
            auto_index=auto_index,
            custom_title=custom_title,
        )

    return mcp_app


# Default module-level router and FastMCP application
router = KBRouter()
mcp = build_mcp_server(router)

# Module-level convenience functions delegating to default router
list_kbs = router.list_kbs
ask = router.ask
retrieve_context = router.retrieve_context
find_documents = router.find_documents
job_status = router.job_status
evidence = router.evidence
ingest_url = router.ingest_url
discover_duplicates = router.discover_duplicates
compare_folders = router.compare_folders
remember = router.remember
search_memory = router.search_memory
deprecate_memory = router.deprecate_memory


# MCP authentication middleware for web connectors and remote endpoints.
# Invariant: Supports Bearer tokens, HTTP Basic auth, X-API-Key, and query parameters
# to allow seamless connection from Claude Web, ChatGPT Custom GPTs, Grok, and Gemini.
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

        # 4. Query Parameter (?token=... or ?key=...)
        if not authorized:
            from urllib.parse import parse_qs
            query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
            params = parse_qs(query_string)
            if self.token in params.get("token", []) or self.token in params.get("key", []):
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