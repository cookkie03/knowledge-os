"""Catalog schema v2, typed sources and registry migration (ticket T4)."""

from __future__ import annotations

import json

import pytest

from storage.catalog import CATALOG_SCHEMA_VERSION, Catalog, source_id_for
from storage.source_migration import migrate_registry_sources


def _write_snapshot(tmp_path, data):
    directory = tmp_path / "kb1"
    directory.mkdir()
    path = directory / "catalog.json"
    path.write_text(json.dumps(data))
    return path


def test_fresh_catalog_is_written_with_current_version(tmp_path):
    with Catalog.open_for_kb("kb1", tmp_path) as catalog:
        catalog.add_source("inbox", "inbox", source_type="inbox")
    snapshot = json.loads((tmp_path / "kb1" / "catalog.json").read_text())
    assert snapshot["version"] == CATALOG_SCHEMA_VERSION == 2


def test_version_one_catalog_is_upgraded_and_keeps_data(tmp_path):
    legacy_source = {"id": "s", "name": "Notes", "origin_host": "mac", "origin_folder": "/notes",
                     "mode": "full_folder", "include_patterns": [], "exclude_patterns": [],
                     "deletion_policy": "guarded", "last_complete_scan_id": None, "status": "active",
                     "created_at": "x", "updated_at": "x"}
    path = _write_snapshot(tmp_path, {"version": 1, "sources": {"s": legacy_source},
                                      "files": {}, "documents": {}, "jobs": {"j": {"id": "j"}}})
    with Catalog.open_for_kb("kb1", tmp_path) as catalog:
        source = catalog.get_source("s")
    assert source["type"] == "folder"
    assert source["path"] == "/notes"
    snapshot = json.loads(path.read_text())
    assert snapshot["version"] == 2
    assert snapshot["jobs"] == {"j": {"id": "j"}}


def test_newer_catalog_is_refused_and_left_untouched(tmp_path):
    path = _write_snapshot(tmp_path, {"version": 3, "sources": {}, "files": {}, "documents": {}, "jobs": {}})
    before = path.read_bytes()
    with Catalog.open_for_kb("kb1", tmp_path) as catalog:
        with pytest.raises(RuntimeError, match="newer"):
            catalog.list_sources()
    assert path.read_bytes() == before


def test_typed_source_validation(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    with Catalog(":memory:") as catalog:
        with pytest.raises(ValueError, match="type"):
            catalog.add_source("x", "x", source_type="remote", path=str(folder))
        with pytest.raises(ValueError, match="absolute"):
            catalog.add_source("x", "x", source_type="folder", path="relative/dir")
        with pytest.raises(ValueError, match="require a path"):
            catalog.add_source("x", "x", source_type="primary")
        with pytest.raises(ValueError, match="must not declare a path"):
            catalog.add_source("inbox", "inbox", source_type="inbox", path=str(folder))

        catalog.add_source("primary", "kb", source_type="primary", path=str(tmp_path))
        catalog.add_source("inbox", "inbox", source_type="inbox")
        with pytest.raises(ValueError, match="primary"):
            catalog.add_source("primary2", "kb", source_type="primary", path=str(folder))
        with pytest.raises(ValueError, match="inbox"):
            catalog.add_source("inbox2", "inbox", source_type="inbox")

        catalog.add_source("f", "docs", source_type="folder", path=str(folder))
        with pytest.raises(ValueError, match="already registered"):
            catalog.add_source("f2", "docs", source_type="folder", path=str(folder / ".." / "docs"))


def test_stored_path_is_resolved_and_findable(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    with Catalog(":memory:") as catalog:
        source = catalog.add_source("f", "docs", source_type="folder", path=str(folder / "sub" / ".."))
        assert source["path"] == str(folder.resolve())
        assert catalog.find_source_by_path(str(folder))["id"] == "f"
        assert catalog.find_source_by_path(str(tmp_path / "missing")) is None


def test_update_source_cannot_change_type_but_revalidates_path(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with Catalog(":memory:") as catalog:
        catalog.add_source("fa", "a", source_type="folder", path=str(a))
        catalog.add_source("fb", "b", source_type="folder", path=str(b))
        with pytest.raises(TypeError):
            catalog.update_source("fa", type="primary")
        with pytest.raises(ValueError, match="already registered"):
            catalog.update_source("fa", path=str(b))
        with pytest.raises(ValueError, match="absolute"):
            catalog.update_source("fa", path="rel")


def test_source_id_for_is_stable(tmp_path):
    assert source_id_for("primary") == "primary"
    assert source_id_for("inbox") == "inbox"
    first = source_id_for("folder", str(tmp_path))
    assert first.startswith("folder-")
    assert first == source_id_for("folder", str(tmp_path / "x" / ".."))
    assert first != source_id_for("folder", str(tmp_path / "other"))


def _kb(tmp_path):
    primary = tmp_path / "primary"
    extra_a = tmp_path / "extra-a"
    extra_b = tmp_path / "extra-b"
    for folder in (primary, extra_a, extra_b):
        folder.mkdir()
    return {"name": "kb1", "source_dir": str(primary), "sources": [str(extra_a), str(extra_b)],
            "exclude": ["node_modules/"]}


def test_migration_dry_run_writes_nothing(tmp_path):
    kb = _kb(tmp_path)
    with Catalog.open_for_kb("kb1", tmp_path / "kbroot") as catalog:
        report = migrate_registry_sources(kb, catalog, dry_run=True)
        assert catalog.list_sources() == []
    assert report["dry_run"] is True
    assert len(report["added"]) == 4
    assert report["existing"] == []
    assert report["conflicts"] == []


def test_migration_adds_sources_and_is_idempotent(tmp_path):
    kb = _kb(tmp_path)
    with Catalog.open_for_kb("kb1", tmp_path / "kbroot") as catalog:
        first = migrate_registry_sources(kb, catalog, dry_run=False)
        sources = catalog.list_sources()
        second = migrate_registry_sources(kb, catalog, dry_run=False)
    types = sorted(source["type"] for source in sources)
    assert types == ["folder", "folder", "inbox", "primary"]
    primary = [source for source in sources if source["type"] == "primary"][0]
    assert primary["exclude_patterns"] == ["node_modules/"]
    assert len(first["added"]) == 4
    assert second["added"] == []
    assert sorted(second["existing"]) == sorted(first["added"])


def test_migration_reports_conflict_and_leaves_existing_source(tmp_path):
    kb = _kb(tmp_path)
    with Catalog.open_for_kb("kb1", tmp_path / "kbroot") as catalog:
        catalog.add_source("manual", "Manual", source_type="folder", path=kb["sources"][0])
        report = migrate_registry_sources(kb, catalog, dry_run=False)
        manual = catalog.get_source("manual")
    assert manual["name"] == "Manual"
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["path"] == kb["sources"][0]
    assert len(report["added"]) == 3
