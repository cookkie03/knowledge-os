"""Moving a KB's data between knowledge-os and a source folder (ticket T7)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

import tools.kb as kb_tools
from storage.catalog import Catalog


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    registry_file = tmp_path / "registry.yaml"
    kb_root = tmp_path / "kbroot"
    monkeypatch.setattr(kb_tools, "REGISTRY", registry_file)
    monkeypatch.setattr(kb_tools, "KB_ROOT", kb_root)
    # write_kb_env also writes under the user's home; keep tests hermetic.
    monkeypatch.setattr(kb_tools, "write_kb_env", lambda kb, env=None: None)
    return {"registry": registry_file, "kb_root": kb_root, "env": {"BASE_PORT": "9700"}, "tmp": tmp_path}


def make_kb(sandbox, name="notes", extra_sources=0):
    source = sandbox["tmp"] / (name + "-src")
    source.mkdir()
    kb = kb_tools.add_kb(name, str(source), env=sandbox["env"])
    derived = Path(kb["derived_dir"])
    (derived / "text").mkdir(parents=True, exist_ok=True)
    (derived / "text" / "a.md").write_text("alpha")
    (Path(kb["rag_storage"]) / "graph.json").write_text('{"nodes": 1}')
    (Path(kb["data_dir"]) / "inbox" / "pending" / "x").mkdir(parents=True, exist_ok=True)
    (Path(kb["data_dir"]) / "inbox" / "pending" / "x" / "f.txt").write_text("queued")
    with Catalog.open_for_kb(name, sandbox["kb_root"]) as catalog:
        catalog.upsert_document("d1", extracted_artifact_path=str(derived / "text" / "a.md"))
    extras = []
    if extra_sources:
        registry = kb_tools.load_registry(sandbox["env"])
        entry = kb_tools.find_kb(registry, name)
        for index in range(extra_sources):
            extra = sandbox["tmp"] / f"{name}-extra{index}"
            extra.mkdir()
            extras.append(extra)
        entry["sources"] = [str(item) for item in extras]
        kb_tools.save_registry(registry)
    return {"kb": kb, "source": source, "extras": extras}


def reload_kb(sandbox, name="notes"):
    return kb_tools.find_kb(kb_tools.load_registry(sandbox["env"]), name)


def move(sandbox, location, name="notes", **kwargs):
    return kb_tools.set_data_location(name, location, env=sandbox["env"], kb_root=sandbox["kb_root"], **kwargs)


def artifact_path(sandbox, name="notes"):
    with Catalog.open_for_kb(name, sandbox["kb_root"]) as catalog:
        return catalog.get_document("d1")["extracted_artifact_path"]


def test_repo_to_source_moves_everything_and_updates_registry_and_catalog(sandbox):
    made = make_kb(sandbox)
    result = move(sandbox, "source")
    target = made["source"].resolve() / ".lightrag"
    assert result["moved"] is True and result["to"] == str(target)
    assert (target / "derived" / "text" / "a.md").read_text() == "alpha"
    assert (target / "rag_storage" / "graph.json").read_text() == '{"nodes": 1}'
    assert (target / "inbox" / "pending" / "x" / "f.txt").read_text() == "queued"
    kb = reload_kb(sandbox)
    assert (kb["data_dir"], kb["derived_dir"], kb["rag_storage"]) == (
        str(target), str(target / "derived"), str(target / "rag_storage"))
    assert result["catalog_paths_updated"] == 1
    assert artifact_path(sandbox) == str(target / "derived" / "text" / "a.md")
    assert not (sandbox["kb_root"] / "notes" / ".lightrag").exists()
    assert (sandbox["kb_root"] / "notes" / "catalog.json").is_file()
    assert not list(target.parent.glob("*.moving"))


def test_source_to_repo_moves_back(sandbox):
    make_kb(sandbox)
    move(sandbox, "source")
    result = move(sandbox, "repo")
    expected = (sandbox["kb_root"] / "notes" / ".lightrag").resolve()
    assert result["moved"] is True and result["to"] == str(expected)
    assert (expected / "derived" / "text" / "a.md").read_text() == "alpha"
    assert artifact_path(sandbox) == str(expected / "derived" / "text" / "a.md")
    assert kb_tools.storage_mode(reload_kb(sandbox)) == "repo"


def test_several_sources_require_an_explicit_choice(sandbox):
    made = make_kb(sandbox, extra_sources=1)
    with pytest.raises(ValueError, match="source"):
        move(sandbox, "source")
    with pytest.raises(ValueError, match="not a source"):
        move(sandbox, "source", source=str(sandbox["tmp"] / "unknown"))
    result = move(sandbox, "source", source=str(made["extras"][0]))
    assert result["to"] == str(made["extras"][0].resolve() / ".lightrag")
    assert (made["extras"][0] / ".lightrag" / "derived" / "text" / "a.md").read_text() == "alpha"


def test_dry_run_changes_nothing(sandbox):
    made = make_kb(sandbox)
    before = reload_kb(sandbox)
    result = move(sandbox, "source", dry_run=True)
    assert result["dry_run"] is True and result["moved"] is False
    assert reload_kb(sandbox) == before
    assert not (made["source"] / ".lightrag").exists()
    assert (Path(before["derived_dir"]) / "text" / "a.md").read_text() == "alpha"


def test_destination_must_be_empty(sandbox):
    made = make_kb(sandbox)
    (made["source"] / ".lightrag").mkdir()
    (made["source"] / ".lightrag" / "keep.txt").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        move(sandbox, "source")


def test_running_server_blocks_the_move(sandbox):
    make_kb(sandbox)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    try:
        registry = kb_tools.load_registry(sandbox["env"])
        kb_tools.find_kb(registry, "notes")["port"] = server.getsockname()[1]
        kb_tools.save_registry(registry)
        with pytest.raises(ValueError, match="stop it first"):
            move(sandbox, "source")
    finally:
        server.close()


def test_pending_checkpoint_and_processing_inbox_block_the_move(sandbox):
    made = make_kb(sandbox)
    checkpoint = Path(made["kb"]["derived_dir"]) / ".checkpoints" / "chk_x.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_text("{}")
    with pytest.raises(ValueError, match="checkpoints"):
        move(sandbox, "source")
    checkpoint.unlink()
    processing = Path(made["kb"]["data_dir"]) / "inbox" / "processing" / "batch"
    processing.mkdir(parents=True)
    with pytest.raises(ValueError, match="processing"):
        move(sandbox, "source")


def test_invalid_location_and_unknown_kb(sandbox):
    make_kb(sandbox)
    with pytest.raises(ValueError, match="location"):
        move(sandbox, "elsewhere")
    with pytest.raises(ValueError, match="not found"):
        move(sandbox, "repo", name="ghost")


def test_moving_to_the_current_location_is_a_noop(sandbox):
    make_kb(sandbox)
    before = reload_kb(sandbox)
    result = move(sandbox, "repo")
    assert result["moved"] is False
    assert reload_kb(sandbox) == before


def test_failed_verification_keeps_old_data_and_registry(sandbox, monkeypatch):
    made = make_kb(sandbox)
    before = reload_kb(sandbox)

    def broken(src, dest):
        raise ValueError("copy verification failed")

    monkeypatch.setattr(kb_tools, "_verify_copy", broken)
    with pytest.raises(ValueError, match="verification"):
        move(sandbox, "source")
    assert reload_kb(sandbox) == before
    assert (Path(before["derived_dir"]) / "text" / "a.md").read_text() == "alpha"
    assert not (made["source"] / ".lightrag").exists()
    assert not list(made["source"].glob("*.moving")) and not list(made["source"].glob(".lightrag*"))


def test_verify_copy_detects_a_changed_byte(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "f.txt").write_text("same")
    (tmp_path / "b" / "f.txt").write_text("sane")
    with pytest.raises(ValueError, match="different"):
        kb_tools._verify_copy(tmp_path / "a", tmp_path / "b")


def test_registry_save_failure_rolls_everything_back(sandbox, monkeypatch):
    made = make_kb(sandbox)
    old_artifact = artifact_path(sandbox)

    def broken_save(registry):
        raise RuntimeError("disk full")

    monkeypatch.setattr(kb_tools, "save_registry", broken_save)
    with pytest.raises(RuntimeError, match="disk full"):
        move(sandbox, "source")
    assert artifact_path(sandbox) == old_artifact
    assert not (made["source"] / ".lightrag").exists()
    assert (Path(made["kb"]["derived_dir"]) / "text" / "a.md").read_text() == "alpha"
