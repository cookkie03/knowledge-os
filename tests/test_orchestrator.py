"""Single ingestion path: scanner -> job -> worker -> Catalog (ticket T5).

Only the remote LightRAG server is faked; parsing, the real ProtectedIndexer and the
Catalog run for real. Tests check external behaviour: catalog files, jobs and inbox dirs.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from pipeline.indexer import KBWriterLock, ProtectedIndexer
from pipeline.ingestion_worker import IngestionWorker
from pipeline.orchestrator import run_ingest
from pipeline.source_scanner import diff_snapshots, hash_source_files
from storage.catalog import Catalog
from storage.inbox import get_inbox_status, load_inbox_jobs, queue_inbox_send


class Backend:
    """Simulate only the remote LightRAG server; indexing, catalog and recovery stay real."""

    def __init__(self):
        self.documents = {}
        self.inserts = 0

    def insert_text(self, text, file_source):
        self.inserts += 1
        self.documents[file_source] = {"id": file_source, "status": "PROCESSED", "content": text}
        return self.documents[file_source]

    def wait_for_processed(self, file_source):
        return self.documents[file_source]

    def delete_documents(self, doc_ids):
        for doc_id in doc_ids:
            self.documents.pop(doc_id, None)
        return True


def _kb(tmp_path):
    data_dir = tmp_path / "kbdata"
    return {"name": "kb1", "port": 9621, "source_dir": str(tmp_path / "src"),
            "data_dir": str(data_dir), "derived_dir": str(data_dir / "derived"),
            "rag_storage": str(data_dir / "rag"), "exclude": []}


def _setup(tmp_path):
    (tmp_path / "src").mkdir()
    kb = _kb(tmp_path)
    catalog = Catalog(":memory:")
    backend = Backend()
    indexer = ProtectedIndexer(catalog, backend, derived_dir=kb["derived_dir"], kb_id="kb1")
    worker = IngestionWorker(catalog, indexer, kb["derived_dir"], staging_dir=tmp_path / "kbdata" / "staging",
                             kb_id="kb1")
    return kb, catalog, backend, worker


def _primary(catalog, tmp_path):
    catalog.add_source("primary", "kb1", source_type="primary", path=str(tmp_path / "src"))


# ---------------------------------------------------------------- scanner helpers

def test_hash_source_files_returns_sha_and_marks_changed_files_incomplete(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    result = hash_source_files(tmp_path, ["a.txt", "missing.txt"])
    assert [f["path"] for f in result["files"]] == ["a.txt"]
    assert result["files"][0]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert result["errors"]


def test_diff_snapshots_detects_rename_and_refuses_deletes_when_incomplete():
    old = [{"path": "a.txt", "sha256": "x" * 64}, {"path": "b.txt", "sha256": "y" * 64}]
    new = [{"path": "c.txt", "sha256": "x" * 64}, {"path": "b.txt", "sha256": "y" * 64}]
    plan = diff_snapshots(old, new, True)
    assert plan["rename"] == [{"old": "a.txt", "new": "c.txt"}]
    assert plan["delete"] == []
    assert diff_snapshots(old, [], False)["delete"] == []


# ---------------------------------------------------------------- folder sources

def test_first_run_adds_files_and_second_run_is_a_noop(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    (tmp_path / "src" / "sub").mkdir()
    (tmp_path / "src" / "sub" / "b.txt").write_text("beta")

    first = run_ingest(kb, catalog, worker)
    assert first["sources"][0]["job"]["stage"] == "COMPLETED"
    assert first["sources"][0]["job"]["counters"]["added"] == 2
    assert sorted(f["relative_path"] for f in catalog.list_files(source_id="primary")) == ["a.md", "sub/b.txt"]

    second = run_ingest(kb, catalog, worker)
    assert second["sources"][0]["status"] == "unchanged"
    assert second["sources"][0]["job"] is None
    assert len(catalog.list_jobs(kb_id="kb1")) == 1


def test_modified_and_renamed_files_use_the_right_actions(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    (tmp_path / "src" / "keep.md").write_text("keep")
    run_ingest(kb, catalog, worker)
    inserts_before = backend.inserts

    (tmp_path / "src" / "keep.md").write_text("keep v2")
    (tmp_path / "src" / "a.md").rename(tmp_path / "src" / "renamed.md")
    result = run_ingest(kb, catalog, worker)

    counters = result["sources"][0]["job"]["counters"]
    assert counters["modified"] == 1 and counters["renamed"] == 1
    # Only the modified file is embedded again; the rename costs no embedding.
    assert backend.inserts == inserts_before + 1
    assert catalog.get_file("primary", "renamed.md")["active"] is True
    assert catalog.get_file("primary", "a.md") is None or catalog.get_file("primary", "a.md")["active"] is False


def test_deleted_file_is_deactivated_only_after_a_complete_scan(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    for name in ("a.md", "b.md", "c.md", "d.md", "e.md", "f.md"):
        (tmp_path / "src" / name).write_text(name)
    (tmp_path / "src" / "bye.md").write_text("bye")
    run_ingest(kb, catalog, worker)

    (tmp_path / "src" / "bye.md").unlink()
    result = run_ingest(kb, catalog, worker)
    assert result["sources"][0]["job"]["counters"]["deleted"] == 1
    assert catalog.get_file("primary", "bye.md")["active"] is False


def test_unreachable_source_never_deletes_documents(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    run_ingest(kb, catalog, worker)

    (tmp_path / "src" / "a.md").unlink()
    (tmp_path / "src").rmdir()
    result = run_ingest(kb, catalog, worker)
    assert result["sources"][0]["status"] == "unreachable"
    assert catalog.get_file("primary", "a.md")["active"] is True


def test_unsupported_files_are_skipped_without_failing_the_job(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    (tmp_path / "src" / "blob.xyz").write_bytes(b"\x00\x01")
    result = run_ingest(kb, catalog, worker)
    assert result["sources"][0]["job"]["stage"] == "COMPLETED"
    assert result["sources"][0]["skipped_unsupported"] == 1
    assert catalog.get_file("primary", "blob.xyz") is None


def test_kb_data_dir_inside_source_is_not_ingested(tmp_path):
    (tmp_path / "src").mkdir()
    data_dir = tmp_path / "src" / ".lightrag"
    kb = {"name": "kb1", "port": 9621, "source_dir": str(tmp_path / "src"), "data_dir": str(data_dir),
          "derived_dir": str(data_dir / "derived"), "rag_storage": str(data_dir / "rag"), "exclude": []}
    catalog = Catalog(":memory:")
    indexer = ProtectedIndexer(catalog, Backend(), derived_dir=kb["derived_dir"], kb_id="kb1")
    worker = IngestionWorker(catalog, indexer, kb["derived_dir"], staging_dir=data_dir / "staging", kb_id="kb1")
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    (data_dir / "derived").mkdir(parents=True, exist_ok=True)
    (data_dir / "derived" / "old.md").write_text("derived artifact")
    run_ingest(kb, catalog, worker)
    paths = [f["relative_path"] for f in catalog.list_files(source_id="primary")]
    assert paths == ["a.md"]


def test_user_exclude_patterns_with_trailing_slash_apply(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    catalog.add_source("primary", "kb1", source_type="primary", path=str(tmp_path / "src"),
                       exclude_patterns=["node_modules/"])
    (tmp_path / "src" / "node_modules").mkdir()
    (tmp_path / "src" / "node_modules" / "x.md").write_text("junk")
    (tmp_path / "src" / "a.md").write_text("alpha")
    run_ingest(kb, catalog, worker)
    assert [f["relative_path"] for f in catalog.list_files(source_id="primary")] == ["a.md"]


def test_two_folder_sources_with_same_relative_path_do_not_collide(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "README.md").write_text("one")
    (two / "README.md").write_text("two")
    catalog.add_source("f1", "one", source_type="folder", path=str(one))
    catalog.add_source("f2", "two", source_type="folder", path=str(two))
    run_ingest(kb, catalog, worker)
    assert catalog.get_file("f1", "README.md")["active"] is True
    assert catalog.get_file("f2", "README.md")["active"] is True


def test_staging_directory_is_removed_after_a_run(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    run_ingest(kb, catalog, worker)
    staging = tmp_path / "kbdata" / "staging"
    assert not staging.exists() or list(staging.iterdir()) == []


def test_source_filter_limits_the_run(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a.md").write_text("one")
    (two / "b.md").write_text("two")
    catalog.add_source("f1", "one", source_type="folder", path=str(one))
    catalog.add_source("f2", "two", source_type="folder", path=str(two))
    result = run_ingest(kb, catalog, worker, source_ids=["f2"])
    assert [entry["source"] for entry in result["sources"]] == ["f2"]
    assert catalog.get_file("f1", "a.md") is None


def test_busy_kb_lock_is_reported_not_raised(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    other = KBWriterLock(worker.indexer.writer_lock.lock_path, kb_id="kb1")
    other.acquire()
    try:
        result = run_ingest(kb, catalog, worker)
    finally:
        other.release()
    assert result["status"] == "locked"
    assert catalog.list_files(source_id="primary") == []


# ---------------------------------------------------------------- inbox as intake queue

def _inbox_setup(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    catalog.add_source("inbox", "inbox", source_type="inbox", mode="selected_files")
    return kb, catalog, backend, worker


def test_inbox_batch_goes_through_the_same_worker_and_is_archived(tmp_path):
    kb, catalog, backend, worker = _inbox_setup(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("from the inbox")
    job = queue_inbox_send(kb, source_path=note)
    assert get_inbox_status(kb)["pending"] == 1

    result = run_ingest(kb, catalog, worker)

    batch = result["inbox"][0]
    assert batch["job"]["stage"] == "COMPLETED"
    inbox_files = catalog.list_files(source_id="inbox")
    assert [f["relative_path"] for f in inbox_files] == [f"{job['job_id']}/note.md"]
    status = get_inbox_status(kb)
    assert status["pending"] == 0 and status["processed"] == 1
    assert (tmp_path / "kbdata" / "inbox" / "processed" / job["job_id"] / "note.md").read_text() == "from the inbox"
    assert load_inbox_jobs(kb)["jobs"][job["job_id"]]["status"] == "completed"


def test_two_batches_with_same_filename_do_not_collide(tmp_path):
    kb, catalog, backend, worker = _inbox_setup(tmp_path)
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "note.md").write_text("one")
    (second / "note.md").write_text("two")
    queue_inbox_send(kb, source_path=first / "note.md")
    queue_inbox_send(kb, source_path=second / "note.md")
    run_ingest(kb, catalog, worker)
    assert len(catalog.list_files(source_id="inbox")) == 2


def test_inbox_never_deletes_previous_documents(tmp_path):
    kb, catalog, backend, worker = _inbox_setup(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("first")
    queue_inbox_send(kb, source_path=note)
    run_ingest(kb, catalog, worker)
    other = tmp_path / "other.md"
    other.write_text("second")
    queue_inbox_send(kb, source_path=other)
    run_ingest(kb, catalog, worker)
    assert len(catalog.list_files(source_id="inbox", active_only=True)) == 2


def test_inbox_creates_its_source_when_missing(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("hello")
    queue_inbox_send(kb, source_path=note)
    run_ingest(kb, catalog, worker)
    assert catalog.get_source("inbox")["type"] == "inbox"


def test_batch_left_in_processing_by_a_crash_is_recovered(tmp_path):
    kb, catalog, backend, worker = _inbox_setup(tmp_path)
    note = tmp_path / "note.md"
    note.write_text("hello")
    job = queue_inbox_send(kb, source_path=note)
    pending = tmp_path / "kbdata" / "inbox" / "pending" / job["job_id"]
    pending.rename(tmp_path / "kbdata" / "inbox" / "processing" / job["job_id"])

    result = run_ingest(kb, catalog, worker)
    assert result["inbox"][0]["job"]["stage"] == "COMPLETED"
    assert get_inbox_status(kb)["processing"] == 0


def test_batch_with_only_unsupported_files_is_moved_to_failed(tmp_path):
    kb, catalog, backend, worker = _inbox_setup(tmp_path)
    blob = tmp_path / "blob.xyz"
    blob.write_bytes(b"\x00")
    job = queue_inbox_send(kb, source_path=blob)
    result = run_ingest(kb, catalog, worker)
    assert result["inbox"][0]["status"] == "failed"
    assert get_inbox_status(kb)["failed"] == 1
    assert load_inbox_jobs(kb)["jobs"][job["job_id"]]["status"] == "failed"
    error = json.loads((tmp_path / "kbdata" / "inbox" / "failed" / job["job_id"] / "error.json").read_text())
    assert "unsupported" in json.dumps(error).lower()


def test_consume_inbox_no_longer_exists():
    import storage.inbox as inbox

    assert not hasattr(inbox, "consume_inbox")


# ---------------------------------------------------------------- crash safety and entry point

def test_job_orphaned_by_a_crash_does_not_block_the_next_run(tmp_path):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")
    catalog.create_job("crashed", "kb1", stage="PROCESSING", source_id="primary")

    result = run_ingest(kb, catalog, worker)

    assert result["recovered_jobs"] == ["crashed"]
    assert catalog.get_job("crashed")["stage"] == "FAILED_RETRYABLE"
    assert result["sources"][0]["job"]["stage"] == "COMPLETED"


def test_worker_exception_leaves_a_final_job_stage(tmp_path, monkeypatch):
    kb, catalog, backend, worker = _setup(tmp_path)
    _primary(catalog, tmp_path)
    (tmp_path / "src" / "a.md").write_text("alpha")

    def boom(job_id, manifest=None):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(worker, "process_job", boom)
    result = run_ingest(kb, catalog, worker)

    assert result["sources"][0]["status"] == "failed"
    assert result["sources"][0]["job"]["stage"] == "FAILED_RETRYABLE"
    monkeypatch.undo()
    # The failed job must not stay active, so the retry runs normally.
    retry = run_ingest(kb, catalog, worker)
    assert retry["sources"][0]["job"]["stage"] == "COMPLETED"


def test_run_kb_reports_server_down_and_imports_registry_sources(tmp_path):
    from pipeline.orchestrator import run_kb

    (tmp_path / "src").mkdir()
    (tmp_path / "extra").mkdir()
    kb = _kb(tmp_path)
    kb["port"] = 1  # nothing listens here, so the health check fails
    kb["sources"] = [str(tmp_path / "extra")]
    kb_root = tmp_path / "kbroot"

    result = run_kb(kb, {}, kb_root=kb_root)

    assert result["status"] == "server_down"
    with Catalog.open_for_kb("kb1", kb_root) as catalog:
        types = sorted(source["type"] for source in catalog.list_sources())
    assert types == ["folder", "inbox", "primary"]
