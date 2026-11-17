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

from pipeline.expansion import expand_archives
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


# Turn a source's physical files into the logical files the Catalog tracks.
# ZIP archives are expanded in work_dir (never next to the source) into member
# entries whose path carries provenance ("docs/a.zip!/inner/b.md"); formats the
# parsers cannot read are skipped; every remaining file is hashed. `prefix` is
# prepended to every logical path (the inbox uses it for the batch id).
# Returns {"entries": [{"path","root","rel","size","mtime_ns","sha256"}],
# "skipped": [paths], "errors": [str], "complete": bool}. An incomplete result
# (unreadable file, rejected archive entry) must never authorize a deletion.
def collect_entries(root, rel_paths, work_dir, prefix=""):
    expansion = expand_archives(root, rel_paths, work_dir)
    errors = list(expansion["errors"])
    skipped = []
    candidates = []
    for rel in expansion["plain"]:
        if get_file_category(rel) == "unsupported":
            skipped.append(prefix + rel)
        else:
            candidates.append({"path": prefix + rel, "root": str(root), "rel": rel})
    for member in expansion["members"]:
        if get_file_category(member["path"]) == "unsupported":
            skipped.append(prefix + member["path"])
        else:
            candidates.append({"path": prefix + member["path"], "root": member["root"], "rel": member["rel"]})

    # Hash each group against its own root: plain files against the source,
    # members against their extraction directory.
    groups = {}
    for candidate in candidates:
        groups.setdefault(candidate["root"], []).append(candidate)
    entries = []
    hash_errors = []
    for group_root, group in groups.items():
        rels = []
        for candidate in group:
            rels.append(candidate["rel"])
        hashed = hash_source_files(group_root, rels)
        hash_errors.extend(hashed["errors"])
        info_by_rel = {}
        for item in hashed["files"]:
            info_by_rel[item["path"]] = item
        for candidate in group:
            info = info_by_rel.get(candidate["rel"])
            if info is None:
                continue
            entry = dict(candidate)
            entry["size"] = info["size"]
            entry["mtime_ns"] = info["mtime_ns"]
            entry["sha256"] = info["sha256"]
            entries.append(entry)
    errors.extend(hash_errors)
    entries.sort(key=_entry_path)
    return {"entries": entries, "skipped": skipped, "errors": errors,
            "complete": expansion["complete"] and not hash_errors}


# Sort key for entries (kept as a named function instead of a lambda).
def _entry_path(entry):
    return entry["path"]


# Scan one folder source, plan the delta against the Catalog and run it as a job.
# Safety: an unreachable or partially scanned source never plans deletions, and
# the worker's own guards (thresholds, empty-root detection) still apply. ZIP
# archives are re-extracted into scratch space on every pass because the Catalog
# tracks their members, not the archive itself; the scratch space and the job's
# staging area are always removed afterwards.
def run_folder_source(kb, catalog, worker, source):
    report = {"source": source["id"], "type": source["type"], "status": "unchanged",
              "job": None, "skipped_unsupported": 0, "errors": []}
    root = source.get("path") or ""
    if not root or not Path(root).is_dir():
        report["status"] = "unreachable"
        report["errors"].append("source folder unavailable: {}".format(root))
        return report

    staging_dir = worker.staging_dir
    work_name = new_job_id("expand")
    job_id = None
    try:
        patterns = scanner_patterns(source.get("exclude_patterns")) + internal_excludes(root, kb)
        listing = list_source_files(root, patterns)
        collected = collect_entries(root, listing["files"], Path(staging_dir) / work_name)
        errors = list(listing["errors"]) + collected["errors"]
        complete = listing["complete"] and collected["complete"]
        report["skipped_unsupported"] = len(collected["skipped"])
        report["errors"] = errors

        previous = []
        for record in catalog.list_files(source_id=source["id"], active_only=True):
            previous.append({"path": record["relative_path"], "sha256": record["binary_sha256"]})
        current = []
        by_path = {}
        for entry in collected["entries"]:
            current.append({"path": entry["path"], "sha256": entry["sha256"]})
            by_path[entry["path"]] = entry
        plan = diff_snapshots(previous, current, complete)
        if not (plan["add"] or plan["modify"] or plan["rename"] or plan["delete"]):
            return report

        job_id = new_job_id("scan")
        catalog.create_job(job_id, kb["name"], source_id=source["id"], scan_id=job_id)
        incoming = Path(staging_dir) / job_id / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        actions = []
        for kind in ("add", "modify"):
            for path in plan[kind]:
                entry = by_path[path]
                try:
                    stage_file(entry["root"], entry["rel"], incoming, path)
                except (OSError, ValueError) as exc:
                    # A file that cannot be staged drops out of this plan and
                    # makes the scan incomplete, which also blocks deletions.
                    errors.append("{}: {}".format(path, exc))
                    complete = False
                    continue
                actions.append(create_staged_action(kind, path, binary_sha256=entry["sha256"],
                                                    size=entry["size"], mtime_ns=entry["mtime_ns"],
                                                    staged_filename=path))
        for item in plan["rename"]:
            entry = by_path[item["new"]]
            actions.append(create_staged_action("rename", item["new"], old_relative_path=item["old"],
                                                binary_sha256=entry["sha256"], size=entry["size"],
                                                mtime_ns=entry["mtime_ns"]))
        # Deletions need a trustworthy full scan; staging failures above may have
        # downgraded `complete` after the plan was made, so re-check here.
        if complete:
            for path in plan["delete"]:
                actions.append(create_staged_action("delete", path))
        manifest = create_staged_job_manifest(job_id, kb["name"], source["id"], scan_id=job_id,
                                              mode="full_folder", scan_complete=complete,
                                              scan_errors=len(errors), actions=actions)
        job = run_job(catalog, worker, job_id, manifest)
        report["errors"] = errors
        report["job"] = job
        report["status"] = status_for_job(job)
        return report
    finally:
        remove_staging(staging_dir, work_name)
        if job_id is not None:
            remove_staging(staging_dir, job_id)


# Turn one inbox batch into a job on the logical inbox source and archive it.
# Batch files get the catalog path "<batch id>/<relative path>", so two batches
# that contain the same file name never collide; ZIPs inside a batch are expanded
# in staging with provenance "<batch>/x.zip!/inner". Inbox jobs use mode
# "selected_files", which the worker treats as push mode: it never deletes.
# A batch is archived as failed if nothing could be ingested or if any file or
# archive entry was rejected, so problems are visible; originals are kept either way.
def run_inbox_batch(kb, catalog, worker, batch_id):
    report = {"batch": batch_id, "status": "skipped", "job": None, "skipped_unsupported": []}
    batch_dir = claim_batch(kb, batch_id)
    if batch_dir is None:
        return report

    staging_dir = worker.staging_dir
    work_name = new_job_id("expand")
    job_id = new_job_id("inbox")
    catalog.create_job(job_id, kb["name"], source_id="inbox", scan_id=job_id)
    try:
        collected = collect_entries(batch_dir, list_batch_files(batch_dir), Path(staging_dir) / work_name,
                                    prefix=batch_id + "/")
        errors = list(collected["errors"])
        skipped = collected["skipped"]
        report["skipped_unsupported"] = skipped
        incoming = Path(staging_dir) / job_id / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        actions = []
        for entry in collected["entries"]:
            existing = catalog.get_file("inbox", entry["path"])
            kind = "add"
            if existing is not None and existing["active"]:
                if existing["binary_sha256"] == entry["sha256"]:
                    continue
                kind = "modify"
            try:
                stage_file(entry["root"], entry["rel"], incoming, entry["path"])
            except (OSError, ValueError) as exc:
                errors.append("{}: {}".format(entry["path"], exc))
                continue
            actions.append(create_staged_action(kind, entry["path"], binary_sha256=entry["sha256"],
                                                size=entry["size"], mtime_ns=entry["mtime_ns"],
                                                staged_filename=entry["path"]))
        if not actions and (skipped or errors):
            catalog.update_job(job_id, stage="FAILED_FINAL", error_summary="no ingestible files in batch")
            reason = "no ingestible files in batch"
            if skipped:
                reason += "; unsupported format: {}".format(", ".join(skipped))
            if errors:
                reason += "; {}".format("; ".join(errors))
            finish_batch(kb, batch_id, False, {"error": reason, "catalog_job": job_id,
                                               "unsupported": skipped})
            report["status"] = "failed"
            return report
        manifest = create_staged_job_manifest(job_id, kb["name"], "inbox", scan_id=job_id,
                                              mode="selected_files", scan_complete=True,
                                              scan_errors=len(errors), actions=actions)
        job = run_job(catalog, worker, job_id, manifest)
    finally:
        remove_staging(staging_dir, work_name)
        remove_staging(staging_dir, job_id)

    ok = job["stage"] == "COMPLETED" and not errors
    details = {"catalog_job": job_id, "counters": job["counters"], "unsupported": skipped}
    if not ok:
        problems = list(errors)
        if job.get("error_summary"):
            problems.append(job["error_summary"])
        details["error"] = "; ".join(problems) or "job ended in stage {}".format(job["stage"])
    finish_batch(kb, batch_id, ok, details)
    report["job"] = job
    if ok:
        report["status"] = "completed"
    elif job["stage"] == "COMPLETED":
        # The job succeeded but some file or archive entry was rejected.
        report["status"] = "failed"
    else:
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
