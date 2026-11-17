"""tools.ingest: the single ingestion API used by CLI and MCP (ticket T7)."""

from __future__ import annotations

import pytest

import tools.ingest as ingest_tools
import tools.kb as kb_tools
from storage.catalog import Catalog


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    registry_file = tmp_path / "registry.yaml"
    kb_root = tmp_path / "kbroot"
    monkeypatch.setattr(kb_tools, "REGISTRY", registry_file)
    monkeypatch.setattr(kb_tools, "KB_ROOT", kb_root)
    monkeypatch.setattr(ingest_tools, "KB_ROOT", kb_root)
    # write_kb_env also writes under the user's home; keep tests hermetic.
    monkeypatch.setattr(kb_tools, "write_kb_env", lambda kb, env=None: None)
    return {"kb_root": kb_root, "env": {"BASE_PORT": "9700", "ENGINE_PYTHON": "/usr/bin/python3"}, "tmp": tmp_path}


def make_kb(sandbox, name="notes", port=None):
    source = sandbox["tmp"] / (name + "-src")
    source.mkdir()
    return kb_tools.add_kb(name, str(source), port=port, env=sandbox["env"])


def common(sandbox):
    return {"env": sandbox["env"], "kb_root": sandbox["kb_root"]}


def test_run_reports_server_down_and_imports_registry_sources(sandbox):
    make_kb(sandbox, port=1)
    report = ingest_tools.run("notes", **common(sandbox))
    assert report["status"] == "server_down"
    sources = ingest_tools.list_sources("notes", **common(sandbox))["sources"]
    assert sorted(source["type"] for source in sources) == ["inbox", "primary"]


def test_run_on_unknown_kb_raises(sandbox):
    with pytest.raises(ValueError, match="not found"):
        ingest_tools.run("ghost", **common(sandbox))


def test_run_all_isolates_a_failing_kb(sandbox, monkeypatch):
    make_kb(sandbox, "good", port=1)
    make_kb(sandbox, "bad", port=2)

    def fake_run_kb(kb, env, kb_root=None, source_ids=None, include_inbox=True, lock_timeout=0.0):
        if kb["name"] == "bad":
            raise RuntimeError("boom")
        return {"kb": kb["name"], "status": "ok"}

    monkeypatch.setattr(ingest_tools, "run_kb", fake_run_kb)
    result = ingest_tools.run_all(**common(sandbox))
    by_name = {}
    for report in result["kbs"]:
        by_name[report["kb"]] = report
    assert by_name["good"]["status"] == "ok"
    assert by_name["bad"]["status"] == "error" and "boom" in by_name["bad"]["error"]


def test_sources_add_list_remove(sandbox):
    make_kb(sandbox, port=1)
    extra = sandbox["tmp"] / "extra"
    extra.mkdir()
    added = ingest_tools.add_source("notes", str(extra), **common(sandbox))
    assert added["id"].startswith("folder-") and added["path"] == str(extra.resolve())
    listed = ingest_tools.list_sources("notes", **common(sandbox))["sources"]
    assert sorted(source["type"] for source in listed) == ["folder", "inbox", "primary"]
    assert all("active_files" in source for source in listed)

    with pytest.raises(ValueError, match="does not exist"):
        ingest_tools.add_source("notes", str(sandbox["tmp"] / "missing"), **common(sandbox))
    with pytest.raises(ValueError, match="already registered"):
        ingest_tools.add_source("notes", str(extra), **common(sandbox))

    assert ingest_tools.remove_source("notes", added["id"], **common(sandbox)) == {"removed": added["id"], "files_detached": 0}
    for protected in ("primary", "inbox"):
        with pytest.raises(ValueError, match="cannot be removed"):
            ingest_tools.remove_source("notes", protected, **common(sandbox))
    with pytest.raises(ValueError, match="not found"):
        ingest_tools.remove_source("notes", "nope", **common(sandbox))


def test_registry_extra_sources_are_listed_as_folders(sandbox):
    make_kb(sandbox, port=1)
    extra = sandbox["tmp"] / "extra"
    extra.mkdir()
    registry = kb_tools.load_registry(sandbox["env"])
    kb_tools.find_kb(registry, "notes")["sources"] = [str(extra)]
    kb_tools.save_registry(registry)
    listed = ingest_tools.list_sources("notes", **common(sandbox))["sources"]
    assert [source["type"] for source in listed].count("folder") == 1


def make_jobs(sandbox):
    with Catalog.open_for_kb("notes", sandbox["kb_root"]) as catalog:
        catalog.create_job("j1", "notes", stage="FAILED_RETRYABLE", source_id="primary")
        catalog.create_job("j2", "notes", stage="COMPLETED", source_id="primary")


def test_jobs_listing_filtering_and_lookup(sandbox):
    make_kb(sandbox, port=1)
    make_jobs(sandbox)
    everything = ingest_tools.list_jobs("notes", **common(sandbox))["jobs"]
    assert sorted(job["job_id"] for job in everything) == ["j1", "j2"]
    done = ingest_tools.list_jobs("notes", stage="COMPLETED", **common(sandbox))["jobs"]
    assert [job["job_id"] for job in done] == ["j2"]
    assert ingest_tools.get_job("notes", "j1", **common(sandbox))["stage"] == "FAILED_RETRYABLE"
    with pytest.raises(ValueError, match="not found"):
        ingest_tools.get_job("notes", "nope", **common(sandbox))


def test_retry_job_runs_a_pass_limited_to_the_jobs_source(sandbox, monkeypatch):
    make_kb(sandbox, port=1)
    make_jobs(sandbox)
    with pytest.raises(ValueError, match="COMPLETED"):
        ingest_tools.retry_job("notes", "j2", **common(sandbox))
    calls = []

    def fake_run(kb_name, **kwargs):
        calls.append((kb_name, kwargs))
        return {"kb": kb_name, "status": "ok"}

    monkeypatch.setattr(ingest_tools, "run", fake_run)
    result = ingest_tools.retry_job("notes", "j1", **common(sandbox))
    assert result["retried"] == "j1" and result["report"]["status"] == "ok"
    assert calls[0][1]["source_ids"] == ["primary"] and calls[0][1]["include_inbox"] is False


def test_inbox_add_and_status(sandbox):
    make_kb(sandbox, port=1)
    note = sandbox["tmp"] / "note.md"
    note.write_text("hello")
    job = ingest_tools.inbox_add("notes", path=str(note), env=sandbox["env"])
    assert job["job_id"]
    status = ingest_tools.inbox_status("notes", env=sandbox["env"])
    assert status["pending"] == 1 and [item["job_id"] for item in status["jobs"]] == [job["job_id"]]
    with pytest.raises(ValueError, match="path or items"):
        ingest_tools.inbox_add("notes", env=sandbox["env"])
    with pytest.raises(ValueError, match="does not exist"):
        ingest_tools.inbox_add("notes", path=str(sandbox["tmp"] / "missing.md"), env=sandbox["env"])


def test_start_background_spawns_the_worker_command(sandbox, monkeypatch):
    make_kb(sandbox, port=1)
    recorded = []

    def fake_spawn(command, cwd, log_path, env=None):
        recorded.append((command, cwd, log_path))
        return 4242

    monkeypatch.setattr(ingest_tools, "spawn_detached_job", fake_spawn)
    result = ingest_tools.start_background("notes", env=sandbox["env"])
    assert result["pid"] == 4242 and result["log"].endswith("ingest.log")
    assert recorded[0][0][-2:] == ["_worker_run", "notes"]
    assert recorded[0][0][0] == "/usr/bin/python3"
