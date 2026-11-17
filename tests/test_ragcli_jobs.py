"""Exercise the job and ingest CLI commands against an isolated catalog."""

import json
import subprocess
from pathlib import Path

import pytest
import ragcli
from storage.catalog import Catalog
from tools import ingest as ingest_tools


# Point tools.ingest at a temporary catalog root and a one-KB registry.
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_tools, "KB_ROOT", tmp_path)
    monkeypatch.setattr(ingest_tools, "global_env", lambda: {})
    monkeypatch.setattr(ingest_tools, "load_registry", lambda env: {"kbs": [{"name": "kb"}]})


def test_cli_wrapper_reports_unknown_kb_without_traceback(tmp_path):
    wrapper = Path(__file__).resolve().parents[1] / "bin" / "ragcli"
    result = subprocess.run([str(wrapper), "job", "list", "--kb", "missing-kb-xyz"], cwd=tmp_path,
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "missing-kb-xyz" in result.stderr


def test_job_wait_returns_failure_for_failed_job(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        catalog.create_job("job", "kb", source_id="source", stage="FAILED_RETRYABLE")
    args = ragcli.build_parser().parse_args(["job", "wait", "job", "--kb", "kb", "--json"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["state"] == "FAILED_RETRYABLE"


def test_job_status_reports_catalog_state(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        catalog.create_job("job", "kb", source_id="source", stage="PROCESSING")
    args = ragcli.build_parser().parse_args(["job", "status", "job", "--kb", "kb", "--json"])
    args.func(args)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["state"] == "PROCESSING"


def test_job_list_filters_by_stage(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        catalog.create_job("a", "kb", source_id="source", stage="COMPLETED")
        catalog.create_job("b", "kb", source_id="source", stage="PROCESSING")
    args = ragcli.build_parser().parse_args(["job", "list", "--kb", "kb", "--stage", "COMPLETED", "--json"])
    args.func(args)
    jobs = json.loads(capsys.readouterr().out)["jobs"]
    assert [job["job_id"] for job in jobs] == ["a"]


def test_removed_commands_are_gone():
    parser = ragcli.build_parser()
    for command in (["sync", "prepare"], ["link", "status", "kb"]):
        with pytest.raises(SystemExit):
            parser.parse_args(command)
