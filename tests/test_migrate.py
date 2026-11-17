import json

from tools import migrate


# Build a registry entry on a temporary folder so no real KB is touched.
def _kb(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    data = tmp_path / "data"
    (data / "derived" / "manifests").mkdir(parents=True)
    (data / "derived" / "manifests" / "m.json").write_text("{}")
    (data / "rag_storage").mkdir()
    (data / "rag_storage" / "kv_store_doc_status.json").write_text(
        json.dumps({"doc-1": {"file_path": "a.md", "status": "processed"}}))
    return {"name": "t", "source_dir": str(source), "data_dir": str(data),
            "derived_dir": str(data / "derived"), "rag_storage": str(data / "rag_storage"), "port": 1}


def _patch(monkeypatch, kb):
    monkeypatch.setattr(migrate, "load_registry", lambda env: {"kbs": [kb]})
    monkeypatch.setattr(migrate, "find_kb", lambda registry, name: kb)


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    kb = _kb(tmp_path)
    _patch(monkeypatch, kb)
    report = migrate.run("t", dry_run=True, env={"x": "y"}, kb_root=tmp_path / "kbroot")
    assert report["backup"] is None
    assert len(report["sources"]["added"]) == 2
    assert [d["lightrag_doc_id"] for d in report["documents"]["unreconciled"]] == ["doc-1"]
    assert not (tmp_path / "data" / "backups").exists()
    assert not (tmp_path / "kbroot" / "t" / "catalog.json").exists()


def test_apply_is_idempotent_and_backs_up(tmp_path, monkeypatch):
    kb = _kb(tmp_path)
    _patch(monkeypatch, kb)
    first = migrate.run("t", dry_run=False, env={"x": "y"}, kb_root=tmp_path / "kbroot")
    assert (tmp_path / "data" / "backups").is_dir()
    assert (tmp_path / "data" / "rag_storage" / "kv_store_doc_status.json").is_file()
    second = migrate.run("t", dry_run=False, env={"x": "y"}, kb_root=tmp_path / "kbroot")
    assert len(first["sources"]["added"]) == 2 and second["sources"]["added"] == []
    assert second["deleted"] == 0
