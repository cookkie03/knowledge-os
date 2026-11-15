"""Inbox queue and lifecycle manager for LightRAG-KB.

Implements a non-blocking, append-only inbox under the private KB data directory
(`<kb_data_dir>/inbox/`). Files are never synced in a destructive mirror mode;
instead, incoming files or directories are queued into `pending/`, moved atomically
to `processing/` during ingestion, and upon success archived into `processed/`
(or `failed/` with diagnostic logs upon error).
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(ROOT / "bin"))

from kb_registry import global_env
from pipeline.parsers import validate_safe_relative_path

logger = logging.getLogger("storage.inbox")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def utc_now_iso() -> str:
    """Return a timezone-aware ISO 8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


# Retrieve and initialize inbox directories under the private KB data directory.
# Invariant: inbox files must stay inside the KB's private data storage, never
# contaminating or deleting external source directories.
def get_inbox_dirs(kb: dict[str, Any]) -> dict[str, Path]:
    data_dir = Path(kb["data_dir"]).resolve()
    inbox_root = data_dir / "inbox"
    dirs = {
        "root": inbox_root,
        "staging": inbox_root / ".staging",
        "pending": inbox_root / "pending",
        "processing": inbox_root / "processing",
        "processed": inbox_root / "processed",
        "failed": inbox_root / "failed",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def _get_jobs_file(kb: dict[str, Any]) -> Path:
    return Path(kb["data_dir"]).resolve() / "inbox_jobs.json"


# Read the persistent jobs snapshot with shared flock locking.
# Prevents partial reads while background workers are updating job records.
def load_inbox_jobs(kb: dict[str, Any]) -> dict[str, Any]:
    path = _get_jobs_file(kb)
    if not path.is_file():
        return {"jobs": {}}
    lock_file = path.with_suffix(".lock")
    try:
        with lock_file.open("a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                content = path.read_text(encoding="utf-8")
                return json.loads(content) if content.strip() else {"jobs": {}}
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning(f"Failed to read inbox jobs for {kb.get('name')}: {exc}")
        return {"jobs": {}}


# Atomically update a job record using flock locking, mkstemp, and os.replace.
# Guarantees crash-safety and atomic updates per AGENTS.md requirements.
def update_inbox_job(kb: dict[str, Any], job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = _get_jobs_file(kb)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_suffix(".lock")

    with lock_file.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = {"jobs": {}}
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8")
                    if content.strip():
                        data = json.loads(content)
                except Exception:
                    data = {"jobs": {}}

            jobs = data.setdefault("jobs", {})
            current = jobs.get(job_id, {
                "job_id": job_id,
                "kb": kb.get("name"),
                "status": "queued",
                "created_at": utc_now_iso(),
            })
            current.update(updates)
            current["updated_at"] = utc_now_iso()
            jobs[job_id] = current

            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".inbox_jobs_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, indent=2, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_path, str(path))
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            return current
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


# Compute SHA256 checksum of an arbitrary file block-by-block.
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Queue incoming local files or directories into pending staging.
# Validates paths, computes file checksums and metadata, and moves atomically.
def queue_inbox_send(
    kb: dict[str, Any],
    source_path: str | Path | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    dirs = get_inbox_dirs(kb)
    job_id = f"job_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    stage_dir = dirs["staging"] / job_id
    stage_dir.mkdir(parents=True, exist_ok=True)

    file_manifests: list[dict[str, Any]] = []

    try:
        # Case 1: Local file or directory path
        if source_path is not None:
            src = Path(source_path).expanduser().resolve()
            if not src.exists():
                raise FileNotFoundError(f"Source path does not exist: {source_path}")

            if src.is_file():
                dest_file = stage_dir / src.name
                shutil.copy2(src, dest_file)
                st = dest_file.stat()
                file_manifests.append({
                    "relative_path": src.name,
                    "sha256": file_sha256(dest_file),
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "ctime_ns": st.st_ctime_ns,
                })
            elif src.is_dir():
                for root_path, _, filenames in os.walk(src):
                    for filename in filenames:
                        full_path = Path(root_path) / filename
                        rel_path = full_path.relative_to(src)
                        dest_file = stage_dir / rel_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(full_path, dest_file)
                        st = dest_file.stat()
                        file_manifests.append({
                            "relative_path": str(rel_path),
                            "sha256": file_sha256(dest_file),
                            "size": st.st_size,
                            "mtime_ns": st.st_mtime_ns,
                            "ctime_ns": st.st_ctime_ns,
                        })

        # Case 2: In-memory/streamed files uploaded over MCP
        elif items is not None:
            for item in items:
                rel_raw = item.get("path") or item.get("relative_path")
                if not rel_raw:
                    continue
                safe_rel = validate_safe_relative_path(rel_raw, base_dir=stage_dir, allow_symlinks=False)
                target = stage_dir / safe_rel
                target.parent.mkdir(parents=True, exist_ok=True)

                content = item.get("content")
                if isinstance(content, str):
                    if item.get("encoding") == "base64":
                        target.write_bytes(base64.b64decode(content))
                    else:
                        target.write_text(content, encoding="utf-8")
                elif isinstance(content, (bytes, bytearray)):
                    target.write_bytes(content)
                else:
                    raise ValueError(f"Unsupported content type for item: {rel_raw}")

                st = target.stat()
                file_manifests.append({
                    "relative_path": safe_rel,
                    "sha256": file_sha256(target),
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "ctime_ns": st.st_ctime_ns,
                })
        else:
            raise ValueError("Either source_path or items must be provided.")

        if not file_manifests:
            raise ValueError("No files found to queue for inbox send.")

        manifest_data = {
            "job_id": job_id,
            "kb": kb.get("name"),
            "created_at": utc_now_iso(),
            "files": file_manifests,
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Atomic promotion to pending
        pending_dest = dirs["pending"] / job_id
        os.replace(stage_dir, pending_dest)

        job_record = update_inbox_job(kb, job_id, {
            "type": "inbox_send",
            "status": "queued",
            "file_count": len(file_manifests),
            "files": [f["relative_path"] for f in file_manifests],
        })
        return job_record

    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


# Process pending batches in the inbox and insert parsed content into LightRAG.
# Moves batches to processing/, parses supported document formats, inserts into
# LightRAG server, and archives to processed/ (or failed/ on error).
def consume_inbox(
    kb: dict[str, Any],
    specific_job_id: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = env or global_env()
    dirs = get_inbox_dirs(kb)
    port = int(kb.get("port", env.get("BASE_PORT", 9621)))

    from bin.ingest import insert_text, render_content  # Lazy import to avoid circular dependencies

    batches_to_run: list[Path] = []
    if specific_job_id:
        target = dirs["pending"] / specific_job_id
        if target.is_dir():
            batches_to_run.append(target)
    else:
        batches_to_run = sorted(
            [p for p in dirs["pending"].iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
        )

    results: list[dict[str, Any]] = []
    if batches_to_run:
        logger.info(f"Starting inbox consume for '{kb.get('name')}' ({len(batches_to_run)} batch(es) queued)")

    for batch in batches_to_run:
        job_id = batch.name
        proc_dir = dirs["processing"] / job_id

        # Atomic transition: pending -> processing
        try:
            os.replace(batch, proc_dir)
        except OSError as exc:
            logger.warning(f"Could not lock batch {job_id} for processing: {exc}")
            continue

        logger.info(f"Processing inbox batch '{job_id}'")
        update_inbox_job(kb, job_id, {
            "status": "processing",
            "started_at": utc_now_iso(),
        })

        manifest_file = proc_dir / "manifest.json"
        manifest = {}
        if manifest_file.is_file():
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}

        processed_files = []
        errors = []

        try:
            for root_path, _, filenames in os.walk(proc_dir):
                for filename in filenames:
                    if filename == "manifest.json":
                        continue
                    file_path = Path(root_path) / filename
                    rel_to_proc = file_path.relative_to(proc_dir)
                    source_tag = f"inbox:{kb.get('name')}:{job_id}:{rel_to_proc}"

                    try:
                        content, _ = render_content(file_path, env)
                        if content and content.strip():
                            status = insert_text(port, content, source=source_tag)
                            processed_files.append({"file": str(rel_to_proc), "status": status})
                        else:
                            processed_files.append({"file": str(rel_to_proc), "status": "empty_skipped"})
                    except Exception as err:
                        errors.append({"file": str(rel_to_proc), "error": str(err)})

            if errors:
                # Failure branch: move to failed/
                dest_dir = dirs["failed"] / job_id
                if dest_dir.exists():
                    shutil.rmtree(dest_dir, ignore_errors=True)
                os.replace(proc_dir, dest_dir)
                err_report = {
                    "job_id": job_id,
                    "failed_at": utc_now_iso(),
                    "errors": errors,
                    "processed": processed_files,
                }
                (dest_dir / "error.json").write_text(
                    json.dumps(err_report, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                rec = update_inbox_job(kb, job_id, {
                    "status": "failed",
                    "completed_at": utc_now_iso(),
                    "errors": errors,
                    "processed_count": len(processed_files),
                })
                logger.error(f"Failed inbox batch '{job_id}' with {len(errors)} error(s)")
                results.append(rec)
            else:
                # Success branch: move to processed/
                dest_dir = dirs["processed"] / job_id
                if dest_dir.exists():
                    shutil.rmtree(dest_dir, ignore_errors=True)
                os.replace(proc_dir, dest_dir)
                rec = update_inbox_job(kb, job_id, {
                    "status": "completed",
                    "completed_at": utc_now_iso(),
                    "processed_count": len(processed_files),
                    "files": processed_files,
                })
                logger.info(f"Completed inbox batch '{job_id}' ({len(processed_files)} file(s) processed)")
                results.append(rec)

        except Exception as unhandled_exc:
            dest_dir = dirs["failed"] / job_id
            if dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
            if proc_dir.exists():
                os.replace(proc_dir, dest_dir)
            err_report = {
                "job_id": job_id,
                "failed_at": utc_now_iso(),
                "error": str(unhandled_exc),
                "traceback": traceback.format_exc(),
            }
            (dest_dir / "error.json").write_text(
                json.dumps(err_report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            rec = update_inbox_job(kb, job_id, {
                "status": "failed",
                "completed_at": utc_now_iso(),
                "error": str(unhandled_exc),
            })
            logger.error(f"Unhandled exception processing inbox batch '{job_id}': {unhandled_exc}")
            results.append(rec)

    if batches_to_run:
        logger.info(f"Inbox consume completed for '{kb.get('name')}': {len(results)} batch(es) processed")
    return {"processed_batches": len(results), "jobs": results}


# Return count and status breakdown of items across all inbox stages.
def get_inbox_status(kb: dict[str, Any]) -> dict[str, Any]:
    dirs = get_inbox_dirs(kb)
    pending_count = len([p for p in dirs["pending"].iterdir() if p.is_dir()])
    processing_count = len([p for p in dirs["processing"].iterdir() if p.is_dir()])
    processed_count = len([p for p in dirs["processed"].iterdir() if p.is_dir()])
    failed_count = len([p for p in dirs["failed"].iterdir() if p.is_dir()])

    return {
        "kb": kb.get("name"),
        "pending": pending_count,
        "processing": processing_count,
        "processed": processed_count,
        "failed": failed_count,
        "total": pending_count + processing_count + processed_count + failed_count,
    }


# Spawn a detached, non-blocking background process.
# Invariant: CLI and MCP calls must return immediately without blocking terminal.
# Enforces PYTHONUNBUFFERED=1 to stream logs in real time to the output file.
def spawn_detached_job(
    cmd: list[str],
    cwd: Path | str,
    log_path: Path | str,
    env: dict[str, str] | None = None,
) -> int:
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    spawn_env = (env.copy() if env else os.environ.copy())
    spawn_env["PYTHONUNBUFFERED"] = "1"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_file.open("ab") as out:
        out.write(f"[{now_str}] === Starting detached job: {' '.join(cmd)} ===\n".encode("utf-8"))
        out.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=spawn_env,
            stdout=out,
            stderr=out,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        out.write(f"[{now_str}] === Detached job running with PID {proc.pid} ===\n".encode("utf-8"))
        out.flush()
    return proc.pid
