"""Retrieval, inventory, memory and inbox operations on one or many KBs.

Single implementation behind the MCP server and the CLI: methods return plain dicts and
never print. Backend (LightRAG HTTP) failures and unknown KBs are reported as structured
{"error": ...} dicts so that no KB ever falls back to another one.
"""

from __future__ import annotations

import json
import logging

from pathlib import Path
from typing import Any, Callable

import requests

from storage.catalog import Catalog
from tools import ingest as ingest_tools
from tools import memory as memory_tools
from tools.kb import find_kb, load_registry, storage_mode
from tools.system import KB_ROOT

logger = logging.getLogger("tools.retrieval")
DEFAULT_KB_ROOT = KB_ROOT

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


def normalize_strategy(strategy: str | None) -> dict[str, Any]:
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


def normalize_context(context: dict[str, Any] | None) -> dict[str, Any]:
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


class Retrieval:
    """KB-scoped operations (retrieval, inventory, memory, inbox) used by every adapter.

    All methods return plain dicts; failures are structured {"error": ...} dicts so that
    adapters can print or serialize them without handling exceptions.
    """

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
        # The Catalog always lives under KB_ROOT/<kb>, the same place tools.ingest uses.
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

    def list_kbs(self) -> dict[str, Any]:
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

        return {"kbs": kbs_list, "total": len(kbs_list)}

    def ask(
        self,
        kb: str,
        question: str,
        strategy: str = "balanced",
        context: dict[str, Any] | None = None,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        """Query a Knowledge Base, returning both LLM answer and provenance references (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

        try:
            mode = normalize_strategy(strategy)
        except ValueError as exc:
            return {
                "error": "INVALID_STRATEGY",
                "kb": kb,
                "strategy": strategy,
                "message": str(exc),
            }

        try:
            normalized_ctx = normalize_context(context)
        except ValueError as exc:
            return {
                "error": "INVALID_CONTEXT",
                "kb": kb,
                "message": str(exc),
            }

        payload: dict[str, Any] = {
            "query": question,
            "mode": mode,
            **normalized_ctx,
        }
        if response_format is not None:
            payload["response_type"] = response_format

        data, err = self._post_backend(kb, "/query", payload)
        if err:
            return err

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

        return result

    def retrieve_context(
        self,
        kb: str,
        question: str,
        strategy: str = "balanced",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve raw context data (original chunks, entities, relations, source IDs) (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

        try:
            mode = normalize_strategy(strategy)
        except ValueError as exc:
            return {
                "error": "INVALID_STRATEGY",
                "kb": kb,
                "strategy": strategy,
                "message": str(exc),
            }

        try:
            normalized_ctx = normalize_context(context)
        except ValueError as exc:
            return {
                "error": "INVALID_CONTEXT",
                "kb": kb,
                "message": str(exc),
            }

        payload = {
            "query": question,
            "mode": mode,
            **normalized_ctx,
        }
        data, err = self._post_backend(kb, "/query/data", payload)
        if err:
            return err

        if isinstance(data, dict):
            is_degraded, degraded_reason = self.check_kb_degraded(kb)
            if is_degraded:
                data["degraded"] = True
                data["warning"] = f"KB '{kb}' è in stato DEGRADED ({degraded_reason})."

        return data

    def find_documents(
        self,
        kb: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
        source: str | None = None,
        path_prefix: str | None = None,
        active_only: bool = True,
    ) -> dict[str, Any]:
        """Query granular inventory with source/path filters and pagination using Catalog (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

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

        return docs_data

    def job_status(self, kb: str, job_id: str) -> dict[str, Any]:
        """Check progress and retry/review state for a sync/ingest job (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

        with self.get_catalog(kb) as cat:
            job = cat.get_job(job_id)
            if job is None:
                return {
                    "error": "JOB_NOT_FOUND",
                    "kb": kb,
                    "job_id": job_id,
                    "message": f"Job '{job_id}' not found in KB '{kb}'.",
                }

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
            return result

    def evidence(self, kb: str, doc_id: str, chunk_id: str | None = None) -> dict[str, Any]:
        """Inspect extracted content, origin path, and metadata for a document or chunk (§6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

        with self.get_catalog(kb) as cat:
            doc = cat.get_document(doc_id)
            if doc is None:
                doc = cat.get_document_by_lightrag_id(doc_id)
            if doc is None:
                return {
                    "error": "DOCUMENT_NOT_FOUND",
                    "kb": kb,
                    "doc_id": doc_id,
                    "message": f"Document '{doc_id}' not found in catalog for KB '{kb}'.",
                }

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

        return result

    def discover_duplicates(self, kb: str, kind: str = "exact", scope: str | None = None) -> dict[str, Any]:
        """Find duplicate clusters across sources in the KB (§6, §8 A1)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

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
            return {
                "clusters": clusters,
                "total_clusters": len(clusters),
                "kind": kind,
                "scope": scope,
            }

        # Future advanced duplicate modules (A2-A5)
        return {
            "error": "FEATURE_NOT_ENABLED",
            "feature": f"discover_duplicates:{kind}",
            "kb": kb,
            "message": (
                f"Duplicate mode '{kind}' is not currently enabled for KB '{kb}'. "
                "Only 'exact' mode (A1 binary SHA256) is active."
            ),
        }

    def compare_folders(
        self, kb: str, left_source: str, right_source: str, mode: str = "novelty"
    ) -> dict[str, Any]:
        """Compare knowledge between two sources in a KB (§6, §8 A6)."""
        kb_entry = self.get_kb(kb)
        if not kb_entry:
            return {
                "error": "KB_NOT_FOUND",
                "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed.",
            }

        # Advanced feature A6 not yet enabled
        return {
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
        }

    def remember(self, kb: str, content: str, memory_type: str = "fact", title: str = "",
                 tags: list[str] | None = None, supersedes_id: str | None = None,
                 source: str = "agent") -> dict[str, Any]:
        """Store an agent memory as Markdown with frontmatter in <source_dir>/memories/."""
        record = self.get_kb(kb)
        if not record:
            return self._not_found(kb)
        try:
            mem = memory_tools.remember_memory(
                source_dir=record.get("source_dir"), content=content, memory_type=memory_type,
                title=title, tags=tags, supersedes_id=supersedes_id, source=source,
            )
            return {"status": "ok", "kb": kb, "memory": mem}
        except Exception as exc:
            return {"error": "REMEMBER_FAILED", "kb": kb, "message": str(exc)}

    def search_memory(self, kb: str, query: str, memory_type: str | None = None,
                      active_only: bool = True, top_k: int = 5) -> dict[str, Any]:
        """Search structured agent memories by query, status and type."""
        record = self.get_kb(kb)
        if not record:
            return self._not_found(kb)
        try:
            results = memory_tools.query_memories(
                source_dir=record.get("source_dir"), query=query, memory_type=memory_type,
                active_only=active_only, top_k=top_k,
            )
            return {"status": "ok", "kb": kb, "count": len(results), "memories": results}
        except Exception as exc:
            return {"error": "SEARCH_MEMORY_FAILED", "kb": kb, "message": str(exc)}

    def deprecate_memory(self, kb: str, memory_id: str, reason: str = "",
                         superseded_by: str | None = None) -> dict[str, Any]:
        """Mark an agent memory as deprecated or superseded."""
        record = self.get_kb(kb)
        if not record:
            return self._not_found(kb)
        try:
            updated = memory_tools.deprecate_memory_by_id(
                source_dir=record.get("source_dir"), memory_id=memory_id, reason=reason,
                superseded_by=superseded_by,
            )
        except Exception as exc:
            return {"error": "DEPRECATE_MEMORY_FAILED", "kb": kb, "message": str(exc)}
        if not updated:
            return {"error": "MEMORY_NOT_FOUND", "kb": kb, "memory_id": memory_id,
                    "message": f"Memory '{memory_id}' not found."}
        return {"status": "ok", "kb": kb, "memory": updated}

    def inbox_send(self, kb: str, items: list[dict[str, Any]] | None = None,
                   source_path: str | None = None) -> dict[str, Any]:
        """Queue uploaded items or a local path in the inbox (processed by the single worker)."""
        if self.get_kb(kb) is None:
            return self._not_found(kb)
        try:
            job = ingest_tools.inbox_add(kb, path=source_path,
                                         items=items if source_path is None else None, env=self.env)
        except Exception as exc:
            return {"error": "INBOX_SEND_FAILED", "kb": kb, "message": str(exc)}
        return {"ok": True, "job_id": job["job_id"], "status": "queued",
                "file_count": job.get("file_count", 0)}

    def inbox_consume(self, kb: str, job_id: str | None = None) -> dict[str, Any]:
        """Start the single worker in the background; job_id is accepted but never selects a batch."""
        if self.get_kb(kb) is None:
            return self._not_found(kb)
        try:
            started = ingest_tools.start_background(kb, env=self.env)
        except Exception as exc:
            return {"error": "INBOX_CONSUME_FAILED", "kb": kb, "message": str(exc)}
        return {"ok": True, "status": "queued", "pid": started["pid"], "job_id": job_id}

    def inbox_status(self, kb: str) -> dict[str, Any]:
        """Return inbox queue counts and jobs."""
        if self.get_kb(kb) is None:
            return self._not_found(kb)
        return ingest_tools.inbox_status(kb, env=self.env)

    def source_ingest(self, kb: str, sources: list[str] | None = None, force: bool = False) -> dict[str, Any]:
        """Start one background ingestion pass over every configured source of the KB."""
        if self.get_kb(kb) is None:
            return self._not_found(kb)
        try:
            started = ingest_tools.start_background(kb, env=self.env)
        except Exception as exc:
            return {"error": "SOURCE_INGEST_FAILED", "kb": kb, "message": str(exc)}
        return {"ok": True, "status": "queued", "pid": started["pid"]}

    @staticmethod
    def _not_found(kb: str) -> dict[str, Any]:
        return {"error": "KB_NOT_FOUND", "kb": kb,
                "message": f"KB '{kb}' not found in registry. No fallback or cross-KB allowed."}
