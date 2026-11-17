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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


# --- Intake queue lifecycle -------------------------------------------------------
# The inbox is only an intake queue: it receives, stages and archives batches.
# Indexing is done by the single ingestion worker (pipeline.orchestrator), which
# claims a batch, feeds its files to the worker as one job, then archives it.

# Return the ids of pending batches, oldest first, so intake order is preserved.
def list_pending_batches(kb: dict[str, Any]) -> list[str]:
    dirs = get_inbox_dirs(kb)
    batches = [p for p in dirs["pending"].iterdir() if p.is_dir()]
    batches.sort(key=lambda p: p.stat().st_mtime)
    return [p.name for p in batches]


# Move batches left in processing/ by a crashed run back to pending/.
# Safe only because the caller holds the single-writer KB lock: no other worker
# can legitimately own a processing batch at this point. Originals are never
# deleted, so a crash cannot lose submitted files. Returns the recovered ids.
def recover_stuck_batches(kb: dict[str, Any]) -> list[str]:
    dirs = get_inbox_dirs(kb)
    recovered = []
    for stuck in sorted(dirs["processing"].iterdir()):
        if not stuck.is_dir():
            continue
        target = dirs["pending"] / stuck.name
        if target.exists():
            continue
        os.replace(stuck, target)
        update_inbox_job(kb, stuck.name, {"status": "queued", "recovered_at": utc_now_iso()})
        recovered.append(stuck.name)
    return recovered


# Atomically claim a pending batch (pending -> processing) and return its path.
# Returns None if the batch vanished or the rename failed. The id is a single
# directory name, never a path, so a crafted id cannot escape the inbox.
def claim_batch(kb: dict[str, Any], job_id: str) -> Path | None:
    if not job_id or job_id in {".", ".."} or Path(job_id).name != job_id or "\\" in job_id:
        return None
    dirs = get_inbox_dirs(kb)
    source = dirs["pending"] / job_id
    target = dirs["processing"] / job_id
    if not source.is_dir():
        return None
    try:
        os.replace(source, target)
    except OSError as exc:
        logger.warning("Could not claim inbox batch %s: %s", job_id, exc)
        return None
    update_inbox_job(kb, job_id, {"status": "processing", "started_at": utc_now_iso()})
    return target


# List a claimed batch's files as relative posix paths, skipping its manifest.
# Symlinks are skipped: a batch is untrusted input and a link could point
# outside the inbox.
def list_batch_files(batch_dir: Path) -> list[str]:
    files = []
    for root_path, dirnames, filenames in os.walk(batch_dir, followlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            full = Path(root_path) / filename
            rel = full.relative_to(batch_dir).as_posix()
            if rel == "manifest.json" or full.is_symlink():
                continue
            files.append(rel)
    return files


# Archive a claimed batch: processing -> processed on success, -> failed with an
# error.json report otherwise. The originals are always preserved in one of the
# two archives, and the inbox job record is updated with the final status.
def finish_batch(kb: dict[str, Any], job_id: str, ok: bool, details: dict[str, Any]) -> dict[str, Any]:
    dirs = get_inbox_dirs(kb)
    source = dirs["processing"] / job_id
    destination = dirs["processed" if ok else "failed"] / job_id
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    if source.exists():
        os.replace(source, destination)
    if not ok and destination.exists():
        report = {"job_id": job_id, "failed_at": utc_now_iso(), **details}
        (destination / "error.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    updates = {"status": "completed" if ok else "failed", "completed_at": utc_now_iso()}
    updates.update(details)
    return update_inbox_job(kb, job_id, updates)


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
