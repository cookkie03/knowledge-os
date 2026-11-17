"""Import a KB's sources from registry.yaml into its Catalog (ticket T4)."""

from __future__ import annotations

from pathlib import Path

from storage.catalog import source_id_for


# Build the list of sources a registry entry implies: one primary on source_dir,
# one folder per extra path in "sources", and the single inbox. Paths are resolved
# here so ids (derived from the resolved path) match what add_source stores.
def _desired_sources(kb):
    excludes = list(kb.get("exclude", []) or [])
    desired = []
    primary_path = str(Path(kb["source_dir"]).expanduser().resolve())
    desired.append({"id": source_id_for("primary"), "type": "primary", "name": kb["name"],
                    "path": primary_path, "exclude": excludes})
    for raw in kb.get("sources", []) or []:
        folder_path = str(Path(raw).expanduser().resolve())
        desired.append({"id": source_id_for("folder", folder_path), "type": "folder",
                        "name": Path(folder_path).name or folder_path, "path": folder_path,
                        "exclude": excludes})
    desired.append({"id": source_id_for("inbox"), "type": "inbox", "name": "inbox",
                    "path": "", "exclude": []})
    return desired


# Copy registry sources into the Catalog. Data-integrity guarantees:
# - idempotent: stable ids mean a second run only reports "existing";
# - additive: existing sources are never modified or deleted;
# - a path already owned by a different source id is reported as a conflict
#   and skipped, so an operator decides instead of the migration guessing;
# - dry_run=True performs every check but writes nothing.
# Returns a plain dict report.
def migrate_registry_sources(kb, catalog, dry_run=True):
    report = {"kb": kb["name"], "dry_run": dry_run, "added": [], "existing": [], "conflicts": []}
    with catalog.transaction():
        for item in _desired_sources(kb):
            if catalog.get_source(item["id"]) is not None:
                report["existing"].append(item["id"])
                continue
            owner = None
            if item["path"]:
                owner = catalog.find_source_by_path(item["path"])
            if owner is not None:
                report["conflicts"].append({"id": item["id"], "path": item["path"],
                                            "reason": f"path already registered by source {owner['id']}"})
                continue
            if item["type"] in ("primary", "inbox"):
                clash = None
                for source in catalog.list_sources():
                    if source.get("type") == item["type"]:
                        clash = source
                        break
                if clash is not None:
                    report["conflicts"].append({"id": item["id"], "path": item["path"],
                                                "reason": f"KB already has a {item['type']} source: {clash['id']}"})
                    continue
            if not dry_run:
                catalog.add_source(item["id"], item["name"], exclude_patterns=item["exclude"],
                                   source_type=item["type"], path=item["path"])
            report["added"].append(item["id"])
    return report
