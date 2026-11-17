"""Single ingestion orchestrator: scanner -> job -> worker -> Catalog (ticket T5).

Every way of getting files into a KB ends here. Folder sources (primary and
extra folders) are scanned, diffed against the Catalog and turned into one job
each; inbox batches are turned into one job each. All jobs run through the same
IngestionWorker, so parsing, safety guards, checkpoints and Catalog writes have
exactly one implementation. The inbox itself is only an intake queue.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pipeline.indexer import KBLockedError, LightRAGClient, ProtectedIndexer
from pipeline.ingestion_worker import (
    IngestionWorker,
    create_staged_action,
    create_staged_job_manifest,
)
from pipeline.parsers import get_file_category, validate_safe_relative_path
from pipeline.source_scanner import diff_snapshots, hash_source_files, list_source_files
from storage.catalog import Catalog, DEFAULT_KB_ROOT
from storage.inbox import (
    claim_batch,
    finish_batch,
    list_batch_files,
    list_pending_batches,
    recover_stuck_batches,
)
from storage.source_migration import migrate_registry_sources

logger = logging.getLogger("pipeline.orchestrator")

# Job stages in which a job is considered to be mutating the KB.
ACTIVE_JOB_STAGES = ("PREPARING", "TRANSFERRING", "VALIDATING", "PROCESSING")


# Build a unique, filesystem-safe job id. Job ids double as staging directory
# names, so they must be a single path component without separators.
def new_job_id(prefix):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return "{}_{}_{}".format(prefix, stamp, uuid.uuid4().hex[:8])


# Convert registry-style exclude patterns ("node_modules/", ".*/") into the
# scanner's component/fnmatch form by dropping the trailing slash.
def scanner_patterns(patterns):
    result = []
    for pattern in patterns or []:
        cleaned = str(pattern).strip().rstrip("/")
        if cleaned:
            result.append(cleaned)
    return result


# Patterns that keep the KB's own data out of its source scan.
# Invariant: when a KB stores its data inside its source folder (".lightrag"),
# that tree holds derived artifacts, checkpoints and the Catalog itself; indexing
# it would feed the KB its own output in an endless loop.
def internal_excludes(source_path, kb):
    root = Path(source_path).resolve()
    patterns = []
    for key in ("data_dir", "derived_dir", "rag_storage"):
        raw = kb.get(key)
        if not raw:
            continue
        try:
            rel = Path(raw).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if rel in ("", "."):
            continue
        patterns.append(rel)
        patterns.append(rel + "/*")
    return patterns


# Remove a job's staging directory, but only if it really is a directory
# directly inside staging_dir (never a symlink and never a path elsewhere).
def remove_staging(staging_dir, job_id):
    if staging_dir is None:
        return
    base = Path(staging_dir).resolve()
    target = base / job_id
    if target.is_symlink() or not target.exists():
        return
    if target.resolve().parent != base:
        return
    shutil.rmtree(target, ignore_errors=True)


# Copy one source file into the job's incoming area and return nothing.
# The copy is what the worker reads, so a file that changes later cannot alter
# an already-verified job; the worker re-checks the checksum against the plan.
def stage_file(source_root, rel, incoming_dir, staged_rel):
    safe_rel = validate_safe_relative_path(staged_rel, base_dir=incoming_dir, allow_symlinks=False)
    source_file = Path(source_root) / rel
    if source_file.is_symlink():
        raise OSError("symlinks are not ingested")
    destination = Path(incoming_dir) / safe_rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, destination)


# Mark jobs left in an active stage by a crashed run as retryable.
# Safe only because the caller holds the KB writer lock: no live writer can own
# these jobs. The worker refuses to start while another job looks active, so an
# orphan would otherwise block the KB forever. Returns the recovered job ids.
def fail_orphaned_jobs(catalog, kb_id):
    recovered = []
    for job in catalog.list_jobs(kb_id=kb_id, limit=None):
        if job["stage"] in ACTIVE_JOB_STAGES:
            catalog.update_job(job["job_id"], stage="FAILED_RETRYABLE",
                               error_summary="interrupted by a crash; the next run retries")
            recovered.append(job["job_id"])
    return recovered


# Run the worker on a prepared job and always leave the job in a final stage.
# If the worker raises, the job is marked FAILED_RETRYABLE so it cannot stay in
# an active stage and block every later run.
def run_job(catalog, worker, job_id, manifest):
    try:
        return worker.process_job(job_id, manifest)
    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        return catalog.update_job(job_id, stage="FAILED_RETRYABLE", error_summary=str(exc))


# Map a final job stage to the status word used in reports.
def status_for_job(job):
    stage = job["stage"]
    if stage == "COMPLETED":
        return "completed"
    if stage == "NEEDS_REVIEW":
        return "needs_review"
    return "failed"


# Scan one folder source, plan the delta against the Catalog and run it as a job.
# Safety: an unreachable or partially scanned source never plans deletions, and
# the worker's own guards (thresholds, empty-root detection) still apply.
def run_folder_source(kb, catalog, worker, source):
    report = {"source": source["id"], "type": source["type"], "status": "unchanged",
              "job": None, "skipped_unsupported": 0, "errors": []}
    root = source.get("path") or ""
    if not root or not Path(root).is_dir():
        report["status"] = "unreachable"
        report["errors"].append("source folder unavailable: {}".format(root))
        return report

    patterns = scanner_patterns(source.get("exclude_patterns")) + internal_excludes(root, kb)
    listing = list_source_files(root, patterns)
    errors = list(listing["errors"])

    supported = []
    for rel in listing["files"]:
        if get_file_category(rel) == "unsupported":
            report["skipped_unsupported"] += 1
        else:
            supported.append(rel)
    hashed = hash_source_files(root, supported)
    errors.extend(hashed["errors"])
    complete = listing["complete"] and not hashed["errors"]

    previous = []
    for record in catalog.list_files(source_id=source["id"], active_only=True):
        previous.append({"path": record["relative_path"], "sha256": record["binary_sha256"]})
    plan = diff_snapshots(previous, hashed["files"], complete)
    report["errors"] = errors
    if not (plan["add"] or plan["modify"] or plan["rename"] or plan["delete"]):
        return report

    by_path = {}
    for item in hashed["files"]:
        by_path[item["path"]] = item
    job_id = new_job_id("scan")
    catalog.create_job(job_id, kb["name"], source_id=source["id"], scan_id=job_id)
    staging_dir = worker.staging_dir
    incoming = Path(staging_dir) / job_id / "incoming"
    try:
        incoming.mkdir(parents=True, exist_ok=True)
        actions = []
        for kind in ("add", "modify"):
            for rel in plan[kind]:
                info = by_path[rel]
                try:
                    stage_file(root, rel, incoming, rel)
                except (OSError, ValueError) as exc:
                    # A file that cannot be staged drops out of this plan and
                    # makes the scan incomplete, which also blocks deletions.
                    errors.append("{}: {}".format(rel, exc))
                    complete = False
                    continue
                actions.append(create_staged_action(kind, rel, binary_sha256=info["sha256"], size=info["size"],
                                                    mtime_ns=info["mtime_ns"], staged_filename=rel))
        for item in plan["rename"]:
            info = by_path[item["new"]]
            actions.append(create_staged_action("rename", item["new"], old_relative_path=item["old"],
                                                binary_sha256=info["sha256"], size=info["size"],
                                                mtime_ns=info["mtime_ns"]))
        if complete:
            for rel in plan["delete"]:
                actions.append(create_staged_action("delete", rel))
        manifest = create_staged_job_manifest(job_id, kb["name"], source["id"], scan_id=job_id,
                                              mode="full_folder", scan_complete=complete,
                                              scan_errors=len(errors), actions=actions)
        job = run_job(catalog, worker, job_id, manifest)
    finally:
        remove_staging(staging_dir, job_id)
    report["errors"] = errors
    report["job"] = job
    report["status"] = status_for_job(job)
    return report


# Turn one inbox batch into a job on the logical inbox source and archive it.
# Batch files get the catalog path "<batch id>/<relative path>", so two batches
# that contain the same file name never collide. Inbox jobs use mode
# "selected_files", which the worker treats as push mode: it never deletes.
def run_inbox_batch(kb, catalog, worker, batch_id):
    report = {"batch": batch_id, "status": "skipped", "job": None, "skipped_unsupported": []}
    batch_dir = claim_batch(kb, batch_id)
    if batch_dir is None:
        return report

    job_id = new_job_id("inbox")
    catalog.create_job(job_id, kb["name"], source_id="inbox", scan_id=job_id)
    staging_dir = worker.staging_dir
    incoming = Path(staging_dir) / job_id / "incoming"
    skipped = []
    errors = []
    actions = []
    try:
        incoming.mkdir(parents=True, exist_ok=True)
        for rel in list_batch_files(batch_dir):
            if get_file_category(rel) == "unsupported":
                skipped.append(rel)
                continue
            staged_rel = "{}/{}".format(batch_id, rel)
            hashed = hash_source_files(batch_dir, [rel])
            if hashed["errors"]:
                errors.extend(hashed["errors"])
                continue
            info = hashed["files"][0]
            existing = catalog.get_file("inbox", staged_rel)
            kind = "add"
            if existing is not None and existing["active"]:
                if existing["binary_sha256"] == info["sha256"]:
                    continue
                kind = "modify"
            try:
                stage_file(batch_dir, rel, incoming, staged_rel)
            except (OSError, ValueError) as exc:
                errors.append("{}: {}".format(rel, exc))
                continue
            actions.append(create_staged_action(kind, staged_rel, binary_sha256=info["sha256"],
                                                size=info["size"], mtime_ns=info["mtime_ns"],
                                                staged_filename=staged_rel))
        report["skipped_unsupported"] = skipped
        if not actions and (skipped or errors):
            catalog.update_job(job_id, stage="FAILED_FINAL", error_summary="no ingestible files in batch")
            reason = "no supported files in batch: unsupported format for {}".format(", ".join(skipped))
            if errors:
                reason = "{}; {}".format(reason, "; ".join(errors))
            finish_batch(kb, batch_id, False, {"error": reason, "catalog_job": job_id,
                                               "unsupported": skipped})
            report["status"] = "failed"
            return report
        manifest = create_staged_job_manifest(job_id, kb["name"], "inbox", scan_id=job_id,
                                              mode="selected_files", scan_complete=True,
                                              scan_errors=len(errors), actions=actions)
        job = run_job(catalog, worker, job_id, manifest)
    finally:
        remove_staging(staging_dir, job_id)

    ok = job["stage"] == "COMPLETED"
    details = {"catalog_job": job_id, "counters": job["counters"], "unsupported": skipped}
    if not ok:
        details["error"] = job.get("error_summary") or "job ended in stage {}".format(job["stage"])
    finish_batch(kb, batch_id, ok, details)
    report["job"] = job
    report["status"] = status_for_job(job)
    return report


# Process every pending inbox batch, oldest first, until none are left.
# New batches may arrive while earlier ones run, so the queue is re-listed; the
# handled set guarantees a batch that cannot be claimed never loops forever.
def run_inbox(kb, catalog, worker):
    recover_stuck_batches(kb)
    if catalog.get_source("inbox") is None:
        catalog.add_source("inbox", "inbox", mode="selected_files", source_type="inbox")
    reports = []
    handled = set()
    while True:
        fresh = []
        for batch_id in list_pending_batches(kb):
            if batch_id not in handled:
                fresh.append(batch_id)
        if not fresh:
            break
        for batch_id in fresh:
            handled.add(batch_id)
            reports.append(run_inbox_batch(kb, catalog, worker, batch_id))
    return reports


# Run one complete ingestion pass over a KB's sources and inbox.
# `worker` carries the catalog, indexer and staging directory, so tests can
# inject a fake LightRAG backend. The whole pass holds the single-writer KB
# lock; if another writer owns it the pass is skipped and reported as "locked"
# instead of raising. `source_ids` limits the pass to those source ids.
def run_ingest(kb, catalog, worker, source_ids=None, include_inbox=True, lock_timeout=0.0):
    report = {"kb": kb["name"], "status": "ok", "sources": [], "inbox": [], "recovered_jobs": []}
    lock = worker.indexer.writer_lock
    try:
        lock.acquire(timeout=lock_timeout)
    except KBLockedError:
        report["status"] = "locked"
        return report
    try:
        worker.indexer.recover_pending_checkpoints()
        report["recovered_jobs"] = fail_orphaned_jobs(catalog, kb["name"])
        for source in catalog.list_sources(status="active"):
            if source["type"] == "inbox":
                continue
            if source_ids is not None and source["id"] not in source_ids:
                continue
            report["sources"].append(run_folder_source(kb, catalog, worker, source))
        if include_inbox and (source_ids is None or "inbox" in source_ids):
            report["inbox"] = run_inbox(kb, catalog, worker)
    finally:
        lock.release()
    return report


# Build the real catalog, LightRAG client and worker for a registry KB and run
# one pass. Registry sources are copied into the Catalog first (additive and
# idempotent), so the Catalog stays the single authority from here on.
# Returns a report; status "server_down" means LightRAG was not reachable and
# nothing was touched.
def run_kb(kb, env, kb_root=None, source_ids=None, include_inbox=True, lock_timeout=0.0):
    catalog = Catalog.open_for_kb(kb["name"], kb_root or DEFAULT_KB_ROOT)
    try:
        migrate_registry_sources(kb, catalog, dry_run=False)
        client = LightRAGClient(port=int(kb["port"]))
        if not client.health():
            return {"kb": kb["name"], "status": "server_down", "sources": [], "inbox": [],
                    "recovered_jobs": []}
        indexer = ProtectedIndexer(catalog, client, derived_dir=kb["derived_dir"], kb_id=kb["name"])
        worker = IngestionWorker(catalog, indexer, kb["derived_dir"],
                                 staging_dir=Path(kb["data_dir"]) / "staging", env=env, kb_id=kb["name"])
        return run_ingest(kb, catalog, worker, source_ids=source_ids, include_inbox=include_inbox,
                          lock_timeout=lock_timeout)
    finally:
        catalog.close()
