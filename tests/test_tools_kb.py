"""Registry, default data location and add_kb (ticket T7)."""

from __future__ import annotations

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
    env = {"BASE_PORT": "9700"}
    return {"registry": registry_file, "kb_root": kb_root, "env": env, "tmp": tmp_path}


def test_add_kb_defaults_data_into_knowledge_os(sandbox):
    source = sandbox["tmp"] / "notes"
    source.mkdir()
    kb = kb_tools.add_kb("notes", str(source), env=sandbox["env"])
    assert kb["data_dir"] == str((sandbox["kb_root"] / "notes" / ".lightrag").resolve())
    assert kb["derived_dir"].endswith("notes/.lightrag/derived")
    assert kb["port"] == 9700
    assert (sandbox["kb_root"] / "notes" / ".lightrag" / "derived" / "text").is_dir()
    assert kb_tools.find_kb(kb_tools.load_registry(sandbox["env"]), "notes")["source_dir"] == str(source.resolve())
    assert storage_mode_is(kb, "repo")


def storage_mode_is(kb, expected):
    return kb_tools.storage_mode(kb) == expected


def test_add_kb_rejects_duplicates_bad_names_and_missing_source(sandbox):
    source = sandbox["tmp"] / "notes"
    source.mkdir()
    kb_tools.add_kb("notes", str(source), env=sandbox["env"])
    with pytest.raises(ValueError, match="already exists"):
        kb_tools.add_kb("notes", str(source), env=sandbox["env"])
    with pytest.raises(ValueError, match="Invalid KB name"):
        kb_tools.add_kb("../evil", str(source), env=sandbox["env"])
    with pytest.raises(ValueError, match="does not exist"):
        kb_tools.add_kb("other", str(sandbox["tmp"] / "missing"), env=sandbox["env"])


def test_second_kb_gets_the_next_free_port(sandbox):
    for name in ("a", "b"):
        (sandbox["tmp"] / name).mkdir()
    first = kb_tools.add_kb("a", str(sandbox["tmp"] / "a"), env=sandbox["env"])
    second = kb_tools.add_kb("b", str(sandbox["tmp"] / "b"), env=sandbox["env"])
    assert (first["port"], second["port"]) == (9700, 9701)


def test_registry_entry_without_data_dir_defaults_to_knowledge_os(sandbox):
    sandbox["registry"].write_text("kbs:\n- name: legacy\n  source_dir: %s\n" % sandbox["tmp"])
    kb = kb_tools.find_kb(kb_tools.load_registry(sandbox["env"]), "legacy")
    assert kb["data_dir"] == str((sandbox["kb_root"] / "legacy" / ".lightrag").resolve())


def test_save_registry_is_atomic_and_leaves_no_temporary_files(sandbox):
    (sandbox["tmp"] / "n").mkdir()
    kb_tools.add_kb("n", str(sandbox["tmp"] / "n"), env=sandbox["env"])
    leftovers = [p.name for p in sandbox["tmp"].iterdir() if p.name.startswith(".registry.yaml.") and not p.name.endswith(".lock")]
    assert leftovers == []


def test_rebase_artifact_paths_only_touches_the_moved_tree(tmp_path):
    old = tmp_path / "old" / "derived"
    sibling = tmp_path / "old" / "derived-other"
    new = tmp_path / "new" / "derived"
    with Catalog(":memory:") as catalog:
        catalog.upsert_document("d1", extracted_artifact_path=str(old / "text" / "a.md"))
        catalog.upsert_document("d2", extracted_artifact_path=str(sibling / "b.md"))
        catalog.upsert_document("d3")
        assert catalog.rebase_artifact_paths(old, new) == 1
        assert catalog.get_document("d1")["extracted_artifact_path"] == str(new.resolve() / "text" / "a.md")
        assert catalog.get_document("d2")["extracted_artifact_path"] == str(sibling / "b.md")
        # Reversible: swapping the arguments restores the original path.
        assert catalog.rebase_artifact_paths(new, old) == 1
        assert catalog.get_document("d1")["extracted_artifact_path"] == str(old.resolve() / "text" / "a.md")
