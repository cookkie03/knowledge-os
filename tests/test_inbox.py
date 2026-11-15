"""Tests for append-only inbox queue lifecycle."""

import json
from pathlib import Path

from storage.inbox import get_inbox_status, load_inbox_jobs, queue_inbox_send, update_inbox_job


def test_queue_file_and_directory_keeps_sources_untouched(tmp_path):
    kb_root = tmp_path / "kb"
    kb = {"name": "demo", "data_dir": str(kb_root)}
    source = tmp_path / "upload"
    (source / "nested").mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "nested" / "b.txt").write_text("b", encoding="utf-8")

    record = queue_inbox_send(kb, source_path=source)

    assert record["status"] == "queued"
    assert record["file_count"] == 2
    assert (source / "a.txt").read_text(encoding="utf-8") == "a"
    pending = kb_root / "inbox" / "pending" / record["job_id"]
    assert (pending / "a.txt").exists()
    assert (pending / "nested" / "b.txt").exists()
    assert (pending / "manifest.json").exists()
    assert get_inbox_status(kb)["pending"] == 1


def test_queue_mcp_items_rejects_traversal(tmp_path):
    kb = {"name": "demo", "data_dir": str(tmp_path / "kb")}
    try:
        queue_inbox_send(kb, items=[{"path": "../escape.txt", "content": "bad"}])
    except Exception as exc:
        assert "traversal" in str(exc).lower() or "absolute" in str(exc).lower()
    else:
        raise AssertionError("unsafe path was accepted")
    assert not (tmp_path / "escape.txt").exists()


def test_job_updates_are_atomic_and_readable(tmp_path):
    kb = {"name": "demo", "data_dir": str(tmp_path / "kb")}
    update_inbox_job(kb, "job-1", {"status": "queued", "file_count": 1})
    jobs = load_inbox_jobs(kb)
    assert jobs["jobs"]["job-1"]["status"] == "queued"
    assert json.loads((Path(kb["data_dir"]) / "inbox_jobs.json").read_text())["jobs"]


def test_spawn_detached_job_writes_timestamps_to_log(tmp_path):
    import re
    from storage.inbox import spawn_detached_job

    log_path = tmp_path / "test.log"
    pid = spawn_detached_job(["python3", "-c", "print('hello from job')"], cwd=tmp_path, log_path=log_path)
    assert pid > 0

    import time
    for _ in range(50):
        if log_path.exists() and "hello from job" in log_path.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)

    content = log_path.read_text(encoding="utf-8")
    assert "=== Starting detached job:" in content
    assert "=== Detached job running with PID" in content
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", content) is not None
