"""Non-destructive migration of an existing KB to the Catalog model (ticket T10).

The migration is idempotent and never deletes anything: it backs up the Catalog
and the manifests, imports the registry sources with stable ids, and reports
what could not be reconciled so an operator decides.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from storage.catalog import Catalog
from storage.source_migration import migrate_registry_sources
from tools.kb import find_kb, load_registry
from tools.system import KB_ROOT, global_env


# Count regular files below a directory without creating anything.
# Used for the inbox report, so a dry run never touches the inbox layout.
def _count_files(directory: Path) -> int:
    total = 0
    if not directory.is_dir():
        return 0
    for item in directory.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += 1
    return total


# Copy the Catalog and the manifests into a timestamped backup folder.
# The backup is a copy, never a move: the originals stay where they are.
def _backup(catalog_path: Path | None, derived_dir: Path, data_dir: Path) -> str:
    target = data_dir / "backups" / ("migrate-" + time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000:06d}")
    target.mkdir(parents=True, exist_ok=False)
    if catalog_path is not None and catalog_path.is_file():
        shutil.copy2(catalog_path, target / "catalog.json")
    manifests = derived_dir / "manifests"
    if manifests.is_dir():
        shutil.copytree(manifests, target / "manifests", symlinks=True)
    return str(target)


# List indexed LightRAG documents that the Catalog does not know yet.
# Reading doc_status is read-only. These documents stay indexed (no re-embedding
# and no deletion); the report only tells the operator what is unreconciled.
def _unreconciled(catalog: Catalog, rag_storage: Path) -> dict:
    status_file = rag_storage / "kv_store_doc_status.json"
    if not status_file.is_file():
        return {"indexed": 0, "unreconciled": []}
    try:
        statuses = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"indexed": 0, "unreconciled": [], "warning": "doc_status unreadable"}
    missing = []
    for doc_id, info in statuses.items():
        if catalog.get_document_by_lightrag_id(doc_id) is None:
            missing.append({"lightrag_doc_id": doc_id, "file_path": info.get("file_path"),
                            "status": info.get("status")})
    return {"indexed": len(statuses), "unreconciled": missing}


# Migrate one KB. With dry_run=True every check runs but nothing is written.
# Returns a plain dict report; raises ValueError for an unknown KB.
def run(kb_name: str, dry_run: bool = True, env: dict[str, str] | None = None,
        kb_root: str | Path | None = None) -> dict:
    env = env or global_env()
    kb = find_kb(load_registry(env), kb_name)
    if kb is None:
        raise ValueError(f"KB '{kb_name}' not found.")
    data_dir = Path(kb["data_dir"]).resolve()
    # A dry run on a KB without a Catalog uses an in-memory one, so it leaves no file behind.
    catalog_file = Path(kb_root or KB_ROOT) / kb_name / "catalog.json"
    if dry_run and not catalog_file.exists():
        catalog = Catalog(":memory:")
    else:
        catalog = Catalog.open_for_kb(kb_name, kb_root or KB_ROOT)
    try:
        backup = None
        if not dry_run:
            backup = _backup(catalog.path, Path(kb["derived_dir"]).resolve(), data_dir)
        sources = migrate_registry_sources(kb, catalog, dry_run=dry_run)
        reconcile = _unreconciled(catalog, Path(kb["rag_storage"]).resolve())
    finally:
        catalog.close()
    inbox = {"pending": _count_files(data_dir / "inbox" / "pending"),
             "processed": _count_files(data_dir / "inbox" / "processed"),
             "failed": _count_files(data_dir / "inbox" / "failed")}
    return {"kb": kb_name, "dry_run": dry_run, "backup": backup, "sources": sources,
            "documents": reconcile, "inbox": inbox, "deleted": 0}
