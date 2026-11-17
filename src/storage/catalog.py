"""Manage one KB's sources, file occurrences, documents and jobs as JSON."""

from __future__ import annotations

import hashlib
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from storage.json_store import JsonStore

# Repository root is three levels up: src/storage/catalog.py -> storage -> src -> repo.
# The CLI already uses <repo>/kb, so the MCP router must resolve the same directory
# or the two entry points would read different catalogs for the same KB.
DEFAULT_KB_ROOT = Path(__file__).resolve().parent.parent.parent / "kb"
# Sources can either mirror a whole folder or a client-selected subset of files.
VALID_SOURCE_MODES = {"full_folder", "selected_files"}
# Every stage a job can be in; used to gate transitions and to detect active jobs.
VALID_JOB_STAGES = {"PREPARING", "TRANSFERRING", "VALIDATING", "PROCESSING", "COMPLETED", "FAILED_RETRYABLE", "FAILED_FINAL", "NEEDS_REVIEW"}
DEFAULT_JOB_COUNTERS = dict.fromkeys(("added", "modified", "renamed", "deleted", "unchanged", "failed", "warnings"), 0)
# The on-disk JSON schema version this code can read and write. Older code must
# never silently rewrite a newer catalog, so upgrade_catalog_data refuses when the
# stored version is higher and only upgrades in place when it is lower.
CATALOG_SCHEMA_VERSION = 2
# Allowed source types: primary (the KB's main source dir), folder (a mirrored
# sub-folder), and inbox (auto-derived location, never configured by callers).
VALID_SOURCE_TYPES = {"primary", "folder", "inbox"}


def source_id_for(source_type, path=""):
    """Return a stable identifier for a source type, used so migrations are idempotent.

    What this does: primary and inbox have fixed ids ("primary"/"inbox") because there
    is at most one of each per KB; folder ids are derived from the resolved path so the
    same folder always maps to the same id across runs.

    Inputs/Outputs: source_type (str in VALID_SOURCE_TYPES), path (str, optional) -> str id
    """
    if source_type == "primary":
        return "primary"
    if source_type == "inbox":
        return "inbox"
    resolved = str(Path(path).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return "folder-" + digest[:12]


def upgrade_catalog_data(data):
    """Upgrade a loaded catalog snapshot to the current schema version, in place.

    What this does: refuses to rewrite a catalog written by newer code (version higher
    than CATALOG_SCHEMA_VERSION raises RuntimeError so no data is silently lost), and
    upgrades version-1 records in place by adding the "type"/"path" fields derived from
    the legacy origin_folder. The upgrade is idempotent: running it on an already-upgraded
    record changes nothing.

    Inputs/Outputs: data (dict, the parsed JSON snapshot) -> None (mutates data in place)
    """
    stored_version = data.get("version", 1)
    if stored_version > CATALOG_SCHEMA_VERSION:
        raise RuntimeError(
            f"catalog version {stored_version} is newer than supported "
            f"({CATALOG_SCHEMA_VERSION}); refusing to rewrite"
        )
    if stored_version >= CATALOG_SCHEMA_VERSION:
        return
    for source in data.get("sources", {}).values():
        if "type" not in source:
            source["type"] = "folder"
            source["path"] = source.get("origin_folder", "")
    data["version"] = CATALOG_SCHEMA_VERSION
#
# What this does: defines the catalog module for managing a single KB's sources, file
# occurrences, documents, and jobs using JSON storage. Specifies the valid source modes
# (full_folder or selected_files), valid job stages, and default job counters. The
# module provides operations for creating, updating, and tracking sources and jobs
# within a KB.
# Inputs/Outputs: constants used across catalog operations for source and job management


def utc_now_iso():
    """Return a timezone-aware timestamp for catalog records.

    What this does: returns the current time in ISO 8601 format with UTC timezone awareness,
    used for consistent timestamping across all catalog records (created_at, updated_at, etc.).

    Inputs/Outputs: none -> str ISO 8601 timezone-aware timestamp string
    """
    return datetime.now(timezone.utc).isoformat()


def generate_canonical_doc_id(binary_sha256, revision=1):
    r"""Derive a synthetic source identifier without exposing the original filename.

    The ID is content-stable: the same binary always maps to the same ID, and a
    revision suffix distinguishes re-inserted content so the old record can be
    retired instead of mutated in place.

    What this does: creates a content-stable document ID from a binary SHA-256 hash.
    The first 16 characters of the lowercased, stripped hash serve as a prefix.
    A revision suffix (_rN) is added when revision > 1 to distinguish re-inserted
    content, allowing the old record to be retired rather than mutated in place.

    Inputs/Outputs: binary_sha256 (str, 64-char hex hash), revision (int, default 1) ->
    str canonical doc ID like "doc_abcdef_r1" or "doc_empty_r2"
    """
    prefix = binary_sha256.strip().lower()[:16] or "empty"
    return f"doc_{prefix}_r{revision}" if revision > 1 else f"doc_{prefix}"


# Deprecated: origin_host/origin_folder are kept only for backward compatibility with
# pre-T4 records (see upgrade_catalog_data). New code should read "type"/"path" instead.

def create_source_record(source_id, name, origin_host="", origin_folder="", mode="full_folder",
                         include_patterns=None, exclude_patterns=None, deletion_policy="guarded",
                         status="active", created_at=None, updated_at=None, last_complete_scan_id=None,
                         source_type="folder", path=""):
    """Create a source record as a plain dict.

    What this does: creates a new source record dict with the provided parameters, using
    the current timestamp if created_at is not supplied. The source record tracks a source
    folder or file pattern set for a KB, including inclusion/exclusion patterns, deletion
    policy, and status. The record is designed to be stored in JSON format alongside other
    KB metadata.

    Inputs/Outputs: all parameters are str or None except mode (str) and status (str),
    include_patterns/exclude_patterns (optional lists), deletion_policy (str) -> dict
    source record ready for JSON storage
    """
    now = created_at or utc_now_iso()
    return {
        "id": source_id,
        "name": name,
        "type": source_type,
        "path": path,
        "origin_host": origin_host,
        "origin_folder": origin_folder,
        "mode": mode,
        "include_patterns": include_patterns or [],
        "exclude_patterns": exclude_patterns or [],
        "deletion_policy": deletion_policy,
        "last_complete_scan_id": last_complete_scan_id,
        "status": status,
        "created_at": now,
        "updated_at": updated_at or now,
    }


# Validate a source path for its type and return the form to store.
# Security: primary/folder paths are always stored resolved (absolute, no "..",
# symlinks followed once here) so later scans cannot be redirected by a crafted
# relative path. The inbox location is derived from the KB data dir and must
# never be supplied by callers, otherwise intake could write outside the KB.
def _validated_source_path(source_type, path):
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(f"Invalid source type: {source_type}")
    if source_type == "inbox":
        if path:
            raise ValueError("inbox sources must not declare a path")
        return ""
    if not path:
        raise ValueError(f"{source_type} sources require a path")
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        raise ValueError("source path must be absolute")
    return str(expanded.resolve())


# Enforce per-KB source invariants: at most one primary, at most one inbox, and
# no two sources on the same resolved path (they would index the same files twice
# under different source ids). `sources` is the live snapshot dict; the caller
# holds the catalog lock. `own_id` is skipped so updates do not clash with themselves.
def _check_source_unique(sources, own_id, source_type, stored_path):
    for other_id, other in sources.items():
        if other_id == own_id:
            continue
        other_type = other.get("type", "folder")
        if source_type in ("primary", "inbox") and other_type == source_type:
            raise ValueError(f"KB already has a {source_type} source: {other_id}")
        if stored_path and other.get("path") == stored_path:
            raise ValueError(f"path already registered by source {other_id}")


def create_document_record(canonical_doc_id, derived_hash=None, extracted_artifact_path=None,
                           lightrag_doc_id=None, lightrag_status="PENDING", parser_version=None,
                           created_at=None, updated_at=None, reference_count=0):
    """Create a document record as a plain dict."""
    now = created_at or utc_now_iso()
    return {
        "canonical_doc_id": canonical_doc_id,
        "derived_hash": derived_hash,
        "extracted_artifact_path": extracted_artifact_path,
        "lightrag_doc_id": lightrag_doc_id,
        "lightrag_status": lightrag_status,
        "parser_version": parser_version,
        "created_at": now,
        "updated_at": updated_at or now,
        "reference_count": reference_count,
    }


def create_file_record(id=None, source_id="", relative_path="", binary_sha256="", size=0,
                       mtime_ns=0, canonical_doc_id=None, active=True,
                       last_seen_scan_id=None, created_at=None, updated_at=None):
    """Create a file record as a plain dict."""
    now = created_at or utc_now_iso()
    return {
        "id": id,
        "source_id": source_id,
        "relative_path": relative_path,
        "binary_sha256": binary_sha256,
        "size": size,
        "mtime_ns": mtime_ns,
        "canonical_doc_id": canonical_doc_id,
        "active": active,
        "last_seen_scan_id": last_seen_scan_id,
        "created_at": now,
        "updated_at": updated_at or now,
    }


def create_job_record(job_id, kb_id, stage="PREPARING", idempotency_key=None, source_id=None,
                      scan_id=None, counters=None, warnings=None, error_summary=None,
                      resume_marker=None, created_at=None, updated_at=None):
    """Create a job record as a plain dict."""
    now = created_at or utc_now_iso()
    return {
        "job_id": job_id,
        "kb_id": kb_id,
        "stage": stage,
        "idempotency_key": idempotency_key,
        "source_id": source_id,
        "scan_id": scan_id,
        "counters": {**DEFAULT_JOB_COUNTERS, **(counters or {})},
        "warnings": list(warnings or []),
        "error_summary": error_summary,
        "resume_marker": resume_marker,
        "created_at": now,
        "updated_at": updated_at or now,
    }


def copy_record(record):
    """Return a shallow copy of a record dict to prevent external mutation."""
    if record is None:
        return None
    return dict(record)


class _CatalogTransaction:
    """Context manager for catalog transactions with nesting support.

    Nested `with catalog.transaction()` calls share the outermost snapshot: only
    the first entry acquires the underlying JsonStore lock and only the last
    exit commits it. This lets helper methods open a transaction without
    re-acquiring the lock or risking a double-write.
    """

    def __init__(self, catalog):
        self._catalog = catalog
        self._data = None
        self._owns_transaction = False

    def __enter__(self):
        if self._catalog._txn_depth == 0:
            # Outermost nesting level: open the real snapshot under the lock.
            self._data = self._catalog._store.__enter__()
            self._owns_transaction = True
            # Upgrade the snapshot to the current schema version before any caller
            # reads or writes it; the upgrade is persisted by the normal commit on exit.
            upgrade_catalog_data(self._data)
        else:
            # Inner nesting level: reuse the already-open snapshot.
            self._data = self._catalog._data
        self._catalog._txn_depth += 1
        self._catalog._data = self._data
        return self._data

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._catalog._txn_depth -= 1
        if self._owns_transaction:
            # Last exit: commit on success or roll back on exception.
            self._catalog._store.__exit__(exc_type, exc_val, exc_tb)
            self._owns_transaction = False
        if self._catalog._txn_depth == 0:
            self._catalog._data = None
        return False


class Catalog:
    """Provide atomic KB catalog operations over versioned JSON snapshots.

    All mutations go through `_snapshot()`, which acquires the JsonStore lock
    and rewrites the file atomically on exit. Callers never touch the file
    directly, so a crash or concurrent writer cannot leave a half-written JSON
    document behind.
    """

    def __init__(self, path):
        """Open a JSON catalog without ever interpreting a legacy SQLite file as JSON."""
        # ":memory:" gives callers an isolated, non-persistent catalog backed by
        # a temp directory that is cleaned up on close.
        self._temporary = tempfile.TemporaryDirectory() if str(path) == ":memory:" else None
        self._path = Path(self._temporary.name) / "catalog.json" if self._temporary else Path(path)
        if self._path.suffix == ".sqlite":
            raise ValueError("SQLite catalogs are not supported; migration is required")
        self._store = JsonStore(self._path)
        self._data = None
        self._txn_depth = 0

    @classmethod
    def open_for_kb(cls, kb_id, kb_root=None):
        """Open the KB snapshot, refusing to bypass unmigrated SQLite data."""
        if not kb_id or kb_id in {".", ".."} or Path(kb_id).name != kb_id or "\\" in kb_id:
            raise ValueError("invalid KB identifier")
        directory = Path(kb_root or DEFAULT_KB_ROOT) / kb_id
        # A leftover SQLite catalog is never silently reinterpreted as JSON: the
        # operator must migrate it first so no data is lost or corrupted.
        if (directory / "catalog.sqlite").exists() and not (directory / "catalog.json").exists():
            raise RuntimeError("SQLite catalog requires an explicit non-destructive migration")
        return cls(directory / "catalog.json")

    @property
    def path(self):
        """Expose the snapshot path, or None for a temporary catalog."""
        return None if self._temporary else self._path

    def __enter__(self):
        """Support scoped catalog use."""
        return self

    def __exit__(self, *args):
        """Release temporary resources at the end of a scope."""
        self.close()

    def close(self):
        """Release temporary resources without touching persistent catalogs."""
        if self._temporary:
            self._temporary.cleanup()
            self._temporary = None

    def transaction(self):
        """Return a context manager for atomic catalog operations."""
        return _CatalogTransaction(self)

    def _snapshot(self):
        """Return a context manager for a snapshot (reuse active or open independent)."""
        return _CatalogTransaction(self)

    def add_source(self, source_id, name, origin_host="", origin_folder="", mode="full_folder",
                   include_patterns=None, exclude_patterns=None, deletion_policy="guarded", status="active",
                   source_type=None, path=""):
        """Register a uniquely identified source in this KB."""
        if mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source mode: {mode}")
        if source_type is None:
            # Legacy untyped call (sync/server and pre-T5 callers): store a folder
            # whose path mirrors origin_folder, without the typed-path checks.
            # Remove this branch once T5/T9 port every caller to typed sources.
            source_type = "folder"
            stored_path = origin_folder
        else:
            stored_path = _validated_source_path(source_type, path)
        now = utc_now_iso()
        source = create_source_record(source_id, name, origin_host, origin_folder, mode,
                                       include_patterns or [], exclude_patterns or [],
                                       deletion_policy, status, created_at=now, updated_at=now,
                                       source_type=source_type, path=stored_path)
        with self._snapshot() as data:
            if source_id in data["sources"]:
                raise ValueError("source already exists")
            # Uniqueness is checked under the snapshot lock so two concurrent
            # writers cannot both register a second primary/inbox or the same folder.
            _check_source_unique(data["sources"], source_id, source_type, stored_path)
            data["sources"][source_id] = source
        return copy_record(source)

    # Return the source registered on this filesystem path, or None. The input is
    # resolved first so "~/x", "/a/../x" and "/x" all match the same stored record.
    def find_source_by_path(self, path):
        if not path:
            return None
        resolved = str(Path(path).expanduser().resolve())
        with self._snapshot() as data:
            for _, source in sorted(data["sources"].items()):
                if source.get("path") == resolved:
                    return copy_record(source)
        return None

    def get_source(self, source_id):
        """Return source metadata, or None if the source is unknown."""
        with self._snapshot() as data:
            value = data["sources"].get(source_id)
            return copy_record(value) if value else None

    def list_sources(self, status=None):
        """List sources in stable identifier order with an optional status filter."""
        with self._snapshot() as data:
            return [copy_record(v) for _, v in sorted(data["sources"].items()) if status is None or v["status"] == status]

    def update_source(self, source_id, **changes):
        """Update source metadata without changing its stable identifier or type."""
        # "type" is deliberately absent: changing a source's type would break the
        # one-primary/one-inbox invariant and the meaning of its file records.
        allowed = {"name", "origin_host", "origin_folder", "mode", "include_patterns",
                   "exclude_patterns", "deletion_policy", "status", "last_complete_scan_id", "path"}
        if set(changes) - allowed:
            raise TypeError("unknown source fields")
        if changes.get("mode") is not None and changes["mode"] not in VALID_SOURCE_MODES:
            raise ValueError("invalid source mode")
        with self._snapshot() as data:
            source = data["sources"].get(source_id)
            if source is None:
                raise KeyError(source_id)
            if changes.get("path") is not None:
                source_type = source.get("type", "folder")
                changes["path"] = _validated_source_path(source_type, changes["path"])
                _check_source_unique(data["sources"], source_id, source_type, changes["path"])
            for k, v in changes.items():
                if v is not None:
                    source[k] = v
            source["updated_at"] = utc_now_iso()
            return copy_record(source)

    def update_source_scan(self, source_id, scan_id):
        """Record the last successfully completed source scan."""
        return self.update_source(source_id, last_complete_scan_id=scan_id)

    def delete_source(self, source_id):
        """Remove a source and its file occurrences, leaving canonical documents intact."""
        with self._snapshot() as data:
            if source_id not in data["sources"]:
                return False
            del data["sources"][source_id]
            data["files"] = {k: v for k, v in data["files"].items() if v["source_id"] != source_id}
            return True

    def _document(self, data, value):
        """Reconstruct the active reference count from file occurrences."""
        if value is None:
            return None
        value = copy_record(value)
        count = 0
        for f in data["files"].values():
            if f["active"] and f.get("canonical_doc_id") == value["canonical_doc_id"]:
                count += 1
        value["reference_count"] = count
        return value

    def upsert_document(self, canonical_doc_id, derived_hash=None, extracted_artifact_path=None,
                        lightrag_doc_id=None, lightrag_status="PENDING", parser_version=None):
        """Create or update a canonical document without losing existing metadata."""
        with self._snapshot() as data:
            now = utc_now_iso()
            doc = data["documents"].get(canonical_doc_id)
            if doc is None:
                doc = create_document_record(canonical_doc_id, created_at=now)
                data["documents"][canonical_doc_id] = doc
            for key, value in (("derived_hash", derived_hash), ("extracted_artifact_path", extracted_artifact_path),
                                ("lightrag_doc_id", lightrag_doc_id), ("parser_version", parser_version)):
                if value is not None:
                    doc[key] = value
            doc["lightrag_status"] = lightrag_status
            doc["updated_at"] = now
            return self._document(data, doc)

    # Rewrite derived-artifact paths after a KB's derived directory moved.
    # Documents store absolute extracted_artifact_path values; without this the
    # recovery and replacement logic would look for artifacts at the old location.
    # Only paths equal to or below old_prefix change (a sibling such as
    # "<old>-other" is never touched), and the operation is idempotent and
    # reversible by swapping the arguments. Returns the number of documents changed.
    def rebase_artifact_paths(self, old_prefix, new_prefix):
        old = str(Path(old_prefix).resolve())
        new = str(Path(new_prefix).resolve())
        changed = 0
        with self._snapshot() as data:
            for doc in data["documents"].values():
                value = doc.get("extracted_artifact_path")
                if value and (value == old or value.startswith(old + os.sep)):
                    doc["extracted_artifact_path"] = new + value[len(old):]
                    changed += 1
        return changed

    def get_document(self, canonical_doc_id):
        """Retrieve a canonical document with its current active reference count."""
        with self._snapshot() as data:
            return self._document(data, data["documents"].get(canonical_doc_id))

    def _find_document(self, field, value):
        """Find a document by an indexed attribute, deterministically."""
        with self._snapshot() as data:
            for _, doc in sorted(data["documents"].items()):
                if doc.get(field) == value:
                    return self._document(data, doc)
            return None

    def get_document_by_lightrag_id(self, lightrag_doc_id):
        """Resolve an upstream document identifier."""
        return self._find_document("lightrag_doc_id", lightrag_doc_id)

    def get_document_by_derived_hash(self, derived_hash):
        """Resolve a derived-content hash to its canonical document."""
        return self._find_document("derived_hash", derived_hash)

    def update_document_status(self, canonical_doc_id, lightrag_status, lightrag_doc_id=None):
        """Record the upstream state without dropping a prior identifier."""
        with self._snapshot() as data:
            doc = data["documents"].get(canonical_doc_id)
            if doc is None:
                raise KeyError(canonical_doc_id)
            doc["lightrag_status"] = lightrag_status
            doc["updated_at"] = utc_now_iso()
            if lightrag_doc_id is not None:
                doc["lightrag_doc_id"] = lightrag_doc_id
            return self._document(data, doc)

    def get_document_reference_count(self, canonical_doc_id, active_only=True):
        """Count file occurrences referencing a document, optionally including inactive ones."""
        with self._snapshot() as data:
            count = 0
            for f in data["files"].values():
                if f.get("canonical_doc_id") == canonical_doc_id and (f["active"] or not active_only):
                    count += 1
            return count

    def delete_document(self, canonical_doc_id):
        """Delete a document only if no occurrence references it."""
        with self._snapshot() as data:
            for f in data["files"].values():
                if f.get("canonical_doc_id") == canonical_doc_id:
                    raise ValueError("document is referenced")
            return data["documents"].pop(canonical_doc_id, None) is not None

    def delete_document_if_unreferenced(self, canonical_doc_id):
        """Atomically delete a document if it has no active references."""
        with self._snapshot() as data:
            for f in data["files"].values():
                if f.get("canonical_doc_id") == canonical_doc_id and f["active"]:
                    return False
            if canonical_doc_id not in data["documents"]:
                return False
            for occurrence in data["files"].values():
                if occurrence.get("canonical_doc_id") == canonical_doc_id:
                    occurrence["canonical_doc_id"] = None
            del data["documents"][canonical_doc_id]
            return True

    @staticmethod
    def _file_key(source_id, relative_path):
        """Encode the composite key without ambiguity for arbitrary path characters."""
        import json
        return json.dumps([source_id, relative_path], ensure_ascii=False)

    def record_file(self, source_id, relative_path, binary_sha256, size, mtime_ns,
                    canonical_doc_id=None, active=True, last_seen_scan_id=None):
        """Create or update an occurrence while preserving its identity and references."""
        with self._snapshot() as data:
            if source_id not in data["sources"] or (canonical_doc_id is not None and canonical_doc_id not in data["documents"]):
                raise ValueError("unknown source or canonical document")
            key = self._file_key(source_id, relative_path)
            now = utc_now_iso()
            if key not in data["files"]:
                next_id = data.get("next_file_id", 1)
                data["next_file_id"] = next_id + 1
                data["files"][key] = create_file_record(id=next_id, source_id=source_id,
                                                         relative_path=relative_path, created_at=now)
            item = data["files"][key]
            item["binary_sha256"] = binary_sha256
            item["size"] = size
            item["mtime_ns"] = mtime_ns
            item["active"] = active
            item["updated_at"] = now
            if canonical_doc_id is not None:
                item["canonical_doc_id"] = canonical_doc_id
            if last_seen_scan_id is not None:
                item["last_seen_scan_id"] = last_seen_scan_id
            return copy_record(item)

    def get_file(self, source_id, relative_path):
        """Retrieve one source-relative occurrence."""
        with self._snapshot() as data:
            item = data["files"].get(self._file_key(source_id, relative_path))
            return copy_record(item) if item else None

    def list_files(self, source_id=None, active_only=True, limit=None, offset=None):
        """List occurrences in stable source and path order."""
        with self._snapshot() as data:
            items = []
            for f in data["files"].values():
                if source_id is not None and f["source_id"] != source_id:
                    continue
                if active_only and not f["active"]:
                    continue
                items.append(f)
            items.sort(key=lambda f: (f["source_id"], f["relative_path"]))
            if limit is not None:
                start = offset or 0
                items = items[start:start + limit]
            return [copy_record(f) for f in items]

    def find_files_by_hash(self, binary_sha256, active_only=True):
        """Find all matching original file occurrences."""
        return [f for f in self.list_files(active_only=active_only) if f["binary_sha256"] == binary_sha256]

    def rename_file(self, source_id, old_relative_path, new_relative_path):
        """Rename an occurrence atomically without changing its document identity."""
        with self._snapshot() as data:
            old = self._file_key(source_id, old_relative_path)
            new = self._file_key(source_id, new_relative_path)
            if old not in data["files"]:
                raise FileNotFoundError(old_relative_path)
            if old != new and new in data["files"] and data["files"][new]["active"]:
                raise FileExistsError(new_relative_path)
            item = data["files"].pop(old)
            item["relative_path"] = new_relative_path
            item["updated_at"] = utc_now_iso()
            data["files"][new] = item
            return copy_record(item)

    def set_file_active(self, source_id, relative_path, active):
        """Activate or deactivate a known occurrence."""
        with self._snapshot() as data:
            item = data["files"].get(self._file_key(source_id, relative_path))
            if item is None:
                return False
            item["active"] = bool(active)
            item["updated_at"] = utc_now_iso()
            return True

    def mark_files_inactive_not_seen(self, source_id, current_scan_id):
        """Deactivate unseen files after the caller has established a complete scan."""
        with self._snapshot() as data:
            result = []
            for item in data["files"].values():
                if item["source_id"] == source_id and item["active"] and item["last_seen_scan_id"] != current_scan_id:
                    result.append(copy_record(item))
                    item["active"] = False
                    item["updated_at"] = utc_now_iso()
            return result

    def delete_file_record(self, source_id, relative_path):
        """Remove a catalog occurrence without modifying the source file."""
        with self._snapshot() as data:
            return data["files"].pop(self._file_key(source_id, relative_path), None) is not None

    def resolve_evidence(self, canonical_doc_id, active_only=True):
        """Resolve a canonical identifier to all original source occurrences."""
        with self._snapshot() as data:
            doc = data["documents"].get(canonical_doc_id)
            if not doc:
                return []
            result = []
            for f in sorted(data["files"].values(), key=lambda x: (x["source_id"], x["relative_path"])):
                if f.get("canonical_doc_id") != canonical_doc_id or (active_only and not f["active"]):
                    continue
                src = data["sources"][f["source_id"]]
                folder, rel, host = src["origin_folder"].rstrip("/"), f["relative_path"].lstrip("/"), src["origin_host"]
                uri = f"{host}:{folder}/{rel}" if host else f"{folder}/{rel}" if folder else rel
                result.append({
                    "canonical_doc_id": canonical_doc_id,
                    "lightrag_doc_id": doc["lightrag_doc_id"],
                    "extracted_artifact_path": doc["extracted_artifact_path"],
                    "source_id": src["id"],
                    "source_name": src["name"],
                    "origin_host": host,
                    "origin_folder": src["origin_folder"],
                    "relative_path": f["relative_path"],
                    "binary_sha256": f["binary_sha256"],
                    "size": f["size"],
                    "mtime_ns": f["mtime_ns"],
                    "active": f["active"],
                    "source_uri": uri,
                })
            return result

    def find_exact_duplicate_clusters(self, active_only=True):
        """Group identical original files with their source metadata."""
        with self._snapshot() as data:
            groups = {}
            for item in sorted(data["files"].values(), key=lambda f: (f["source_id"], f["relative_path"])):
                if not item["binary_sha256"] or (active_only and not item["active"]):
                    continue
                source = data["sources"][item["source_id"]]
                occurrence = {key: item[key] for key in ("source_id", "relative_path", "canonical_doc_id", "size", "mtime_ns", "active")}
                occurrence["source_name"] = source["name"]
                occurrence["origin_host"] = source["origin_host"]
                occurrence["origin_folder"] = source["origin_folder"]
                groups.setdefault(item["binary_sha256"], []).append(occurrence)
            clusters = []
            for key, items in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])):
                if len(items) >= 2:
                    clusters.append({"binary_sha256": key, "count": len(items), "occurrences": items})
            return clusters

    def create_job(self, job_id, kb_id, stage="PREPARING", idempotency_key=None, source_id=None,
                   scan_id=None, counters=None, warnings=None, error_summary=None, resume_marker=None):
        """Create a uniquely identified job with an optional unique idempotency key."""
        if stage not in VALID_JOB_STAGES:
            raise ValueError("Invalid job stage")
        with self._snapshot() as data:
            if job_id in data["jobs"] or (idempotency_key is not None and any(j["idempotency_key"] == idempotency_key for j in data["jobs"].values())):
                raise ValueError("duplicate job or idempotency key")
            now = utc_now_iso()
            job = create_job_record(job_id, kb_id, stage, idempotency_key, source_id, scan_id,
                                     counters, warnings, error_summary, resume_marker,
                                     created_at=now, updated_at=now)
            data["jobs"][job_id] = job
            return copy_record(job)

    def get_job(self, job_id):
        """Retrieve durable job progress by identifier."""
        with self._snapshot() as data:
            value = data["jobs"].get(job_id)
            return copy_record(value) if value else None

    def get_job_by_idempotency_key(self, idempotency_key):
        """Find a job previously submitted with an idempotency key."""
        with self._snapshot() as data:
            for j in data["jobs"].values():
                if j["idempotency_key"] == idempotency_key:
                    return copy_record(j)
            return None

    def update_job(self, job_id, stage=None, counters=None, warnings=None, error_summary=None, resume_marker=None):
        """Atomically update job progress and merge supplied counters."""
        if stage is not None and stage not in VALID_JOB_STAGES:
            raise ValueError("Invalid job stage")
        with self._snapshot() as data:
            job = data["jobs"].get(job_id)
            if job is None:
                raise KeyError(job_id)
            for key, value in (("stage", stage), ("warnings", warnings), ("error_summary", error_summary), ("resume_marker", resume_marker)):
                if value is not None:
                    job[key] = value
            if counters is not None:
                job["counters"].update(counters)
            job["updated_at"] = utc_now_iso()
            return copy_record(job)

    def list_jobs(self, kb_id=None, source_id=None, stage=None, limit=50):
        """List recent jobs with optional KB, source and stage filters."""
        with self._snapshot() as data:
            matching_jobs = []
            for job in data["jobs"].values():
                if kb_id is not None and job["kb_id"] != kb_id:
                    continue
                if source_id is not None and job["source_id"] != source_id:
                    continue
                if stage is not None and job["stage"] != stage:
                    continue
                matching_jobs.append(job)

            matching_jobs.sort(key=lambda job: job["created_at"], reverse=True)
            if limit is not None:
                matching_jobs = matching_jobs[:limit]

            return [copy_record(job) for job in matching_jobs]

    def increment_job_counter(self, job_id, counter_name, delta=1):
        """Increment a job counter under the same lock as the snapshot write."""
        with self._snapshot() as data:
            job = data["jobs"].get(job_id)
            if job is None:
                raise KeyError(job_id)
            job["counters"][counter_name] = job["counters"].get(counter_name, 0) + delta
            job["updated_at"] = utc_now_iso()
            return copy_record(job)

    def find_documents(self, status=None, source_id=None, path_prefix=None, active_only=True, page=1, page_size=50):
        """Return a bounded page of documents with matching occurrences."""
        page, page_size = max(1, page), max(1, min(page_size, 500))
        with self._snapshot() as data:
            ids = set()
            for f in data["files"].values():
                if not f.get("canonical_doc_id"):
                    continue
                if active_only and not f["active"]:
                    continue
                if source_id is not None and f["source_id"] != source_id:
                    continue
                if path_prefix is not None and not f["relative_path"].startswith(path_prefix):
                    continue
                ids.add(f["canonical_doc_id"])
            docs = []
            for key, doc in sorted(data["documents"].items()):
                if key in ids and (status is None or doc["lightrag_status"] == status):
                    docs.append(self._document(data, doc).copy())
            total = len(docs)
            start = (page - 1) * page_size
            end = page * page_size
            return {"items": docs[start:end], "total": total, "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size))}