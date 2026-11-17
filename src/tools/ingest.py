"""The single implementation of every ingestion operation.

CLI and MCP adapters only call these functions, so they never print, never call
sys.exit and never read argv: they take explicit arguments, return plain dicts and
raise ValueError with a clear message for bad input.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.orchestrator import run_kb
from storage.catalog import Catalog, source_id_for
from storage.inbox import get_inbox_status, load_inbox_jobs, queue_inbox_send, spawn_detached_job
from storage.source_migration import migrate_registry_sources
from tools.kb import find_kb, load_registry, merged_runtime_env
from tools.system import HOME, KB_ROOT, global_env


# Return the registry entry of a KB or raise ValueError.
def _get_kb(name: str, env: dict[str, str] | None) -> dict:
    registry = load_registry(env or global_env())
    kb = find_kb(registry, name)
    if kb is None:
        raise ValueError(f"KB '{name}' not found.")
    return kb


# Open the Catalog of a KB; callers close it in a finally block.
def _open_catalog(name: str, kb_root: str | Path | None) -> Catalog:
    return Catalog.open_for_kb(name, kb_root or KB_ROOT)


# Run one ingestion pass (folder sources plus inbox) on a KB and return the report.
# `source_ids` limits the pass; a busy KB lock is reported, not raised.
def run(kb_name: str, source_ids: list[str] | None = None, include_inbox: bool = True,
        lock_timeout: float = 0.0, env: dict[str, str] | None = None,
        kb_root: str | Path | None = None) -> dict:
    env = env or global_env()
    kb = _get_kb(kb_name, env)
    return run_kb(kb, merged_runtime_env(kb, env), kb_root=kb_root, source_ids=source_ids,
                  include_inbox=include_inbox, lock_timeout=lock_timeout)


# Run a pass on every KB in registry order. A KB that fails is reported with
# status "error" and never prevents the others from running.
def run_all(source_ids: list[str] | None = None, include_inbox: bool = True,
            lock_timeout: float = 0.0, env: dict[str, str] | None = None,
            kb_root: str | Path | None = None) -> dict:
    env = env or global_env()
    registry = load_registry(env)
    reports = []
    for kb in registry["kbs"]:
        try:
            reports.append(run_kb(kb, merged_runtime_env(kb, env), kb_root=kb_root, source_ids=source_ids,
                                  include_inbox=include_inbox, lock_timeout=lock_timeout))
        except Exception as exc:
            reports.append({"kb": kb["name"], "status": "error", "error": str(exc)})
    return {"kbs": reports}


# Start one detached worker pass for a KB and return immediately with its pid and log.
# Adapters use this instead of building worker commands themselves.
def start_background(kb_name: str, env: dict[str, str] | None = None) -> dict:
    env = env or global_env()
    kb = _get_kb(kb_name, env)
    log_path = Path(kb["data_dir"]) / "ingest.log"
    command = [env["ENGINE_PYTHON"], str(HOME / "src" / "cli" / "ragcli.py"), "_worker_run", kb_name]
    pid = spawn_detached_job(command, kb["data_dir"], log_path, env=merged_runtime_env(kb, env))
    return {"kb": kb_name, "pid": pid, "log": str(log_path)}


# List jobs of a KB, newest first, optionally filtered by stage.
def list_jobs(kb_name: str, stage: str | None = None, limit: int = 50, env: dict[str, str] | None = None,
              kb_root: str | Path | None = None) -> dict:
    _get_kb(kb_name, env)
    catalog = _open_catalog(kb_name, kb_root)
    try:
        return {"kb": kb_name, "jobs": catalog.list_jobs(kb_id=kb_name, stage=stage, limit=limit)}
    finally:
        catalog.close()


# Return one job record or raise ValueError.
def get_job(kb_name: str, job_id: str, env: dict[str, str] | None = None,
            kb_root: str | Path | None = None) -> dict:
    _get_kb(kb_name, env)
    catalog = _open_catalog(kb_name, kb_root)
    try:
        job = catalog.get_job(job_id)
    finally:
        catalog.close()
    if job is None:
        raise ValueError(f"Job '{job_id}' not found.")
    return job


# Retry a failed job. Only FAILED_RETRYABLE jobs can be retried. A retry is a
# fresh pass limited to the job's source: the scan recomputes the delta, so
# nothing is replayed blindly from the failed attempt.
def retry_job(kb_name: str, job_id: str, lock_timeout: float = 0.0, env: dict[str, str] | None = None,
              kb_root: str | Path | None = None) -> dict:
    job = get_job(kb_name, job_id, env=env, kb_root=kb_root)
    if job["stage"] != "FAILED_RETRYABLE":
        raise ValueError(f"Job '{job_id}' is in stage {job['stage']}; only FAILED_RETRYABLE jobs can be retried.")
    source_id = job["source_id"]
    source_ids = [source_id] if source_id else None
    include_inbox = source_id == "inbox" or source_id is None
    report = run(kb_name, source_ids=source_ids, include_inbox=include_inbox, lock_timeout=lock_timeout,
                 env=env, kb_root=kb_root)
    return {"retried": job_id, "report": report}


# List a KB's sources with their active file counts. The registry's sources are
# imported into the Catalog first (additive), so the Catalog is the authority.
def list_sources(kb_name: str, env: dict[str, str] | None = None, kb_root: str | Path | None = None) -> dict:
    kb = _get_kb(kb_name, env)
    catalog = _open_catalog(kb_name, kb_root)
    try:
        migrate_registry_sources(kb, catalog, dry_run=False)
        sources = []
        for source in catalog.list_sources():
            entry = dict(source)
            entry["active_files"] = len(catalog.list_files(source_id=source["id"], active_only=True))
            sources.append(entry)
        return {"kb": kb_name, "sources": sources}
    finally:
        catalog.close()


# Register an extra folder as a source of a KB. The path is resolved first and
# must be an existing directory; a path that another source already owns is
# rejected by the Catalog.
def add_source(kb_name: str, path: str, name: str | None = None, exclude_patterns: list[str] | None = None,
               env: dict[str, str] | None = None, kb_root: str | Path | None = None) -> dict:
    kb = _get_kb(kb_name, env)
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"Source directory does not exist: {resolved}")
    catalog = _open_catalog(kb_name, kb_root)
    try:
        migrate_registry_sources(kb, catalog, dry_run=False)
        owner = catalog.find_source_by_path(str(resolved))
        if owner is not None:
            raise ValueError(f"Path already registered by source '{owner['id']}'.")
        return catalog.add_source(source_id_for("folder", str(resolved)), name or resolved.name,
                                  source_type="folder", path=str(resolved),
                                  exclude_patterns=exclude_patterns or [])
    finally:
        catalog.close()


# Detach a folder source from a KB. The primary and inbox sources cannot be
# removed. Canonical documents stay indexed; only the source's file occurrences
# are detached.
def remove_source(kb_name: str, source_id: str, env: dict[str, str] | None = None,
                  kb_root: str | Path | None = None) -> dict:
    _get_kb(kb_name, env)
    catalog = _open_catalog(kb_name, kb_root)
    try:
        source = catalog.get_source(source_id)
        if source is None:
            raise ValueError(f"Source '{source_id}' not found.")
        if source["type"] in ("primary", "inbox"):
            raise ValueError(f"The {source['type']} source cannot be removed.")
        detached = len(catalog.list_files(source_id=source_id, active_only=False))
        catalog.delete_source(source_id)
        return {"removed": source_id, "files_detached": detached}
    finally:
        catalog.close()


# Queue a file, a directory or uploaded items in the inbox. Processing happens
# later in the single worker. Returns the inbox job record.
def inbox_add(kb_name: str, path: str | None = None, items: list[dict] | None = None,
              env: dict[str, str] | None = None) -> dict:
    kb = _get_kb(kb_name, env)
    if path is None and items is None:
        raise ValueError("Provide a path or items to add to the inbox.")
    try:
        return queue_inbox_send(kb, source_path=path, items=items)
    except FileNotFoundError as exc:
        raise ValueError(str(exc))


# Sort key for inbox job records: creation time (named function, no lambda).
def _created_at(record: dict) -> str:
    return record.get("created_at", "")


# Return inbox queue counts plus the inbox jobs, newest first.
def inbox_status(kb_name: str, env: dict[str, str] | None = None) -> dict:
    kb = _get_kb(kb_name, env)
    status = dict(get_inbox_status(kb))
    jobs = list(load_inbox_jobs(kb)["jobs"].values())
    jobs.sort(key=_created_at, reverse=True)
    status["jobs"] = jobs
    return status
