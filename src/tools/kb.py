"""KB registry: one implementation for creating, finding, configuring KBs.

The registry (config/registry.yaml) keeps per-KB configuration only; sources and
files live in each KB's Catalog. Data location, exclude rules and the per-KB
runtime .env are managed here.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import socket
from pathlib import Path
from pathlib import PurePosixPath

import yaml

from tools.system import (
    KB_ROOT,
    REGISTRY,
    atomic_write_text,
    global_env,
    is_relative_to,
    load_env,
)

DERIVED_SUBDIRS = ("text", "pdf", "images", "office", "audio", "cache", "manifests")


# Build a normalized KB configuration dictionary.
# Invariant: Each KB holds configuration for its directories, network port,
# exclusion patterns, linked sources, and background link automation toggles.
# The returned dict is used as the authoritative KB configuration; all pipeline
# components read from this dict so there is a single source of truth for KB
# parameters such as port, exclude patterns, and linkage settings.
def create_kb_entry(name: str, source_dir: str, data_dir: str, derived_dir: str, rag_storage: str,
                    port: int, exclude: list[str] | None = None,
                    sources: list[str] | None = None,
                    link: dict[str, bool] | None = None) -> dict:
    """Create a KB entry as a plain dict."""
    default_link = {"consume": True, "ingest": True}
    if link is not None:
        default_link.update(link)
    return {
        "name": name,
        "source_dir": source_dir,
        "data_dir": data_dir,
        "derived_dir": derived_dir,
        "rag_storage": rag_storage,
        "port": port,
        "exclude": list(exclude or []),
        "sources": list(sources or []),
        "link": default_link,
    }


def normalize_kb(entry: dict, env: dict[str, str] | None = None) -> dict:
    env = env or global_env()
    source_raw = entry.get("source_dir") or entry.get("source_folder")
    if not source_raw:
        raise ValueError(f"KB '{entry.get('name', '?')}' missing source_dir/source_folder")
    source_dir = Path(source_raw).expanduser().resolve()
    data_raw = entry.get("data_dir")
    # Default location of a KB's data: kb/<name>/.lightrag inside knowledge-os (never
    # inside the source folder unless the operator moves it there with
    # set_data_location). The Catalog (kb/<name>/catalog.json) stays one level up,
    # outside the data directory, so moving the data can never move or lose it.
    data_dir = Path(data_raw).expanduser().resolve() if data_raw else (KB_ROOT / entry["name"] / ".lightrag").resolve()
    derived_dir = Path(entry.get("derived_dir") or (data_dir / "derived")).expanduser().resolve()
    rag_storage = Path(entry.get("rag_storage") or (data_dir / "rag_storage")).expanduser().resolve()
    return create_kb_entry(
        name=entry["name"],
        source_dir=str(source_dir),
        data_dir=str(data_dir),
        derived_dir=str(derived_dir),
        rag_storage=str(rag_storage),
        port=int(entry.get("port") or env["BASE_PORT"]),
        exclude=entry.get("exclude", []),
        sources=entry.get("sources", []),
        link=entry.get("link"),
    )


def load_registry(env: dict[str, str] | None = None) -> dict:
    env = env or global_env()
    if not REGISTRY.exists():
        return {"kbs": []}
    data = yaml.safe_load(REGISTRY.read_text()) or {}
    kbs = []
    for entry in data.get("kbs", []):
        kbs.append(normalize_kb(entry, env))
    return {"kbs": kbs}


def save_registry(data: dict) -> None:
    header = "# LightRAG-KB Registry (managed by ragcli, hand-editable).\n"
    payload = {"kbs": [_registry_entry(kb) for kb in data.get("kbs", [])]}
    # Atomic replace: a crash while saving must never truncate the registry.
    atomic_write_text(REGISTRY, header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))


def _registry_entry(kb: dict) -> dict:
    entry = {
        "name": kb["name"],
        "source_dir": kb["source_dir"],
        "data_dir": kb["data_dir"],
        "derived_dir": kb["derived_dir"],
        "rag_storage": kb["rag_storage"],
        "port": int(kb["port"]),
    }
    if kb.get("exclude"):
        entry["exclude"] = list(kb["exclude"])
    if kb.get("sources"):
        entry["sources"] = list(kb["sources"])
    if kb.get("link") is not None:
        entry["link"] = dict(kb["link"])
    return entry


def find_kb(registry: dict, name: str) -> dict | None:
    for kb in registry.get("kbs", []):
        if kb["name"] == name:
            return kb
    return None


def next_port(registry: dict, env: dict[str, str] | None = None) -> int:
    env = env or global_env()
    used = set()
    for kb in registry.get("kbs", []):
        used.add(int(kb["port"]))
    port = int(env["BASE_PORT"])
    while port in used:
        port += 1
    return port


def ensure_kb_dirs(kb: dict) -> None:
    data_dir = Path(kb["data_dir"])
    derived_dir = Path(kb["derived_dir"])
    rag_storage = Path(kb["rag_storage"])
    data_dir.mkdir(parents=True, exist_ok=True)
    derived_dir.mkdir(parents=True, exist_ok=True)
    rag_storage.mkdir(parents=True, exist_ok=True)
    for subdir in DERIVED_SUBDIRS:
        (derived_dir / subdir).mkdir(parents=True, exist_ok=True)


def storage_mode(kb: dict) -> str:
    data_dir = Path(kb["data_dir"])
    source_dir = Path(kb["source_dir"])
    return "source" if is_relative_to(data_dir, source_dir) else "repo"


def split_exclude_specs(raw_patterns: list[str]) -> list[str]:
    specs: list[str] = []
    for raw in raw_patterns:
        for chunk in raw.replace(";", ",").split(","):
            spec = chunk.strip()
            if spec:
                specs.append(spec)
    return specs


def canonicalize_exclude_specs(specs: list[str]) -> tuple[list[str], bool]:
    canonical: list[str] = []
    seen: set[str] = set()
    changed = False
    for spec in specs:
        normalized = spec.strip()
        if normalized.endswith("/*") and not normalized.endswith("/**"):
            normalized = normalized[:-1]
        if normalized != spec:
            changed = True
        if normalized in seen:
            changed = True
            continue
        seen.add(normalized)
        canonical.append(normalized)
    return canonical, changed


def normalize_exclude_specs_for_kb(specs: list[str], source_dir: Path) -> tuple[list[str], bool]:
    canonical, changed = canonicalize_exclude_specs(specs)
    normalized: list[str] = []
    seen: set[str] = set()
    for spec in canonical:
        upgraded = spec
        if "/" not in spec and not any(char in spec for char in "*?[]"):
            candidate = source_dir / spec
            try:
                if candidate.is_dir():
                    upgraded = f"{spec}/"
            except OSError:
                pass
        if upgraded != spec:
            changed = True
        if upgraded in seen:
            changed = True
            continue
        seen.add(upgraded)
        normalized.append(upgraded)
    return normalized, changed


# Enumerate source directories while excluding the data tree and symlink cycles.
def _source_rel_dirs(source_dir: Path, data_dir: Path) -> list[str]:
    data_root = data_dir.resolve()
    rel_dirs: list[str] = []
    seen_dirs: set[Path] = set()
    for root, dirs, _files in os.walk(source_dir, topdown=True, followlinks=True):
        root_path = Path(root)
        try:
            root_resolved = root_path.resolve()
        except OSError:
            dirs[:] = []
            continue
        if root_resolved in seen_dirs:
            dirs[:] = []
            continue
        seen_dirs.add(root_resolved)

        kept_dirs: list[str] = []
        for dirname in dirs:
            child = root_path / dirname
            try:
                child_resolved = child.resolve()
            except OSError:
                continue
            if is_relative_to(child_resolved, data_root):
                continue
            kept_dirs.append(dirname)
            rel_dirs.append(child.relative_to(source_dir).as_posix())
        dirs[:] = kept_dirs
    rel_dirs.sort()
    return rel_dirs


# Check if a folder path matches an exclusion rule.
# Invariant: If folder_spec does not contain a slash, it matches any folder segment
# in the path hierarchy (e.g. '.venv' excludes nested virtualenvs at any depth).
def _folder_rule_matches(rel_dir: str, folder_spec: str) -> bool:
    candidate = PurePosixPath(rel_dir)
    if "/" in folder_spec:
        return fnmatch.fnmatch(rel_dir, folder_spec)
    return any(fnmatch.fnmatch(part, folder_spec) for part in candidate.parts)


def _ancestor_dirs(rel_path: str) -> list[str]:
    path = PurePosixPath(rel_path)
    parents = list(path.parents[:-1])
    result: list[str] = []
    for parent in reversed(parents):
        p = parent.as_posix()
        if p not in {".", ""}:
            result.append(p)
    return result


def dir_is_excluded(rel_dir: str, specs: list[str]) -> bool:
    canonical, _ = canonicalize_exclude_specs(specs)
    candidates = _ancestor_dirs(rel_dir) + [rel_dir]
    for spec in canonical:
        if not spec.endswith("/"):
            continue
        folder_spec = spec[:-1]
        for candidate in candidates:
            if _folder_rule_matches(candidate, folder_spec):
                return True
    return False


def path_is_excluded(rel_path: str, specs: list[str]) -> bool:
    canonical, _ = canonicalize_exclude_specs(specs)
    basename = PurePosixPath(rel_path).name
    for spec in canonical:
        if spec.endswith("/"):
            if any(_folder_rule_matches(candidate, spec[:-1]) for candidate in _ancestor_dirs(rel_path)):
                return True
            continue
        if "/" in spec:
            if fnmatch.fnmatch(rel_path, spec):
                return True
            continue
        if fnmatch.fnmatch(basename, spec):
            return True
    return False


# Expand folder rules against source directories, preserving unmatched rules.
def expand_exclude_specs(specs: list[str], source_dir: Path, data_dir: Path) -> list[str]:
    canonical, _ = canonicalize_exclude_specs(specs)
    expanded: list[str] = []
    seen: set[str] = set()
    source_dirs = _source_rel_dirs(source_dir, data_dir)
    for spec in canonical:
        if not spec.endswith("/"):
            if spec not in seen:
                seen.add(spec)
                expanded.append(spec)
            continue
        folder_spec = spec[:-1]
        matched = False
        for rel_dir in source_dirs:
            if _folder_rule_matches(rel_dir, folder_spec):
                display = f"{rel_dir}/"
                if display in seen:
                    continue
                seen.add(display)
                expanded.append(display)
                matched = True
        if not matched and spec not in seen:
            seen.add(spec)
            expanded.append(spec)
    return expanded


def write_kb_env(kb: dict, env: dict[str, str] | None = None) -> Path:
    env = env or global_env()
    ensure_kb_dirs(kb)
    provider = env.get("LLM_PROVIDER", "ollama")
    if provider == "openrouter":
        llm_binding = "openai"
        llm_host = env["OPENROUTER_HOST"]
        llm_model = env["OPENROUTER_LLM_MODEL"]
        llm_api_key = env.get("OPENROUTER_API_KEY", "")
        embedding_binding = "openai"
        embedding_host = env["OPENROUTER_HOST"]
        embedding_model = env["OPENROUTER_EMBEDDING_MODEL"]
        embedding_dim = env["OPENROUTER_EMBEDDING_DIM"]
        embedding_api_key = llm_api_key
        embedding_use_base64 = env.get("EMBEDDING_USE_BASE64", "false")
    else:
        llm_binding = "ollama"
        llm_host = env["OLLAMA_HOST"]
        llm_model = env["LLM_MODEL"]
        llm_api_key = ""
        embedding_binding = "ollama"
        embedding_host = env["OLLAMA_HOST"]
        embedding_model = env["EMBEDDING_MODEL"]
        embedding_dim = env["EMBEDDING_DIM"]
        embedding_api_key = ""
        embedding_use_base64 = env.get("EMBEDDING_USE_BASE64", "true")

    lines = [
        "# Generated by ragcli — do not edit by hand.",
        f"ENGINE_PYTHON={env['ENGINE_PYTHON']}",
        f"LIGHTRAG_SERVER_BIN={env['LIGHTRAG_SERVER_BIN']}",
        f"DOCLING_PYTHON={env['DOCLING_PYTHON']}",
        f"DOCLING_LANG={env['DOCLING_LANG']}",
        f"DOCLING_TIMEOUT={env['DOCLING_TIMEOUT']}",
        f"MCP_BIND_HOST={env.get('MCP_BIND_HOST', '127.0.0.1')}",
        f"MCP_BIND_PORT={env.get('MCP_BIND_PORT', '8743')}",
        f"MCP_PATH={env.get('MCP_PATH', '/mcp')}",
        f"KB_NAME={kb['name']}",
        "HOST=127.0.0.1",
        f"PORT={kb['port']}",
        f"SOURCE_DIR={kb['source_dir']}",
        f"DATA_DIR={kb['data_dir']}",
        f"DERIVED_DIR={kb['derived_dir']}",
        f"WORKING_DIR={kb['rag_storage']}",
        f"INPUT_DIR={kb['source_dir']}",
        f"RAG_STORAGE_DIR={kb['rag_storage']}",
        f"LLM_BINDING={llm_binding}",
        f"LLM_BINDING_HOST={llm_host}",
        f"LLM_MODEL={llm_model}",
        f"EMBEDDING_BINDING={embedding_binding}",
        f"EMBEDDING_BINDING_HOST={embedding_host}",
        f"EMBEDDING_MODEL={embedding_model}",
        f"EMBEDDING_DIM={embedding_dim}",
        f"EMBEDDING_USE_BASE64={embedding_use_base64}",
        f"RERANK_BINDING={env['RERANK_BINDING']}",
        f"LLM_TIMEOUT={env['LLM_TIMEOUT']}",
        f"TIMEOUT={env['TIMEOUT']}",
        f"MAX_ASYNC={env['MAX_ASYNC']}",
        f"EMBEDDING_TIMEOUT={env['EMBEDDING_TIMEOUT']}",
        f"SUMMARY_LANGUAGE={env['SUMMARY_LANGUAGE']}",
        f"MAX_GLEANING={env['MAX_GLEANING']}",
        f"CHUNK_SIZE={env['CHUNK_SIZE']}",
        f"CHUNK_OVERLAP_SIZE={env['CHUNK_OVERLAP_SIZE']}",
        f"COSINE_THRESHOLD={env['COSINE_THRESHOLD']}",
    ]
    if provider == "ollama":
        lines.extend(
            [
                f"OLLAMA_LLM_NUM_CTX={env['OLLAMA_LLM_NUM_CTX']}",
                f"OLLAMA_LLM_NUM_PREDICT={env['OLLAMA_LLM_NUM_PREDICT']}",
                f"OLLAMA_EMBEDDING_NUM_CTX={env['OLLAMA_EMBEDDING_NUM_CTX']}",
            ]
        )
    if llm_api_key:
        lines.append(f"LLM_BINDING_API_KEY={llm_api_key}")
    if embedding_api_key:
        lines.append(f"EMBEDDING_BINDING_API_KEY={embedding_api_key}")
    env_path = Path(kb["data_dir"]) / ".env"
    text = "\n".join(lines) + "\n"
    env_path.write_text(text)
    service_env_path = Path.home() / ".config" / "lightrag-kb" / f"{kb['name']}.env"
    service_env_path.parent.mkdir(parents=True, exist_ok=True)
    service_env_path.write_text(text)
    return env_path


def merged_runtime_env(kb: dict, env: dict[str, str] | None = None) -> dict[str, str]:
    env = env or global_env()
    merged = os.environ.copy()
    merged.update(env)
    merged.update(load_env(Path(kb["data_dir"]) / ".env"))
    return merged


# Create and register a new KB whose data lives in knowledge-os (kb/<name>/).
# Validates the source folder, refuses duplicate names and picks the next free
# port. Side effects: creates the data directories, writes the per-KB runtime
# env and saves the registry atomically. Returns the new KB dict.
def add_kb(name: str, source_dir: str, port: int | None = None, env: dict[str, str] | None = None) -> dict:
    env = env or global_env()
    if not name or Path(name).name != name or name in {".", ".."} or "\\" in name:
        raise ValueError(f"Invalid KB name: {name!r}")
    registry = load_registry(env)
    if find_kb(registry, name):
        raise ValueError(f"KB '{name}' already exists.")
    source = Path(source_dir).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    data_dir = (KB_ROOT / name / ".lightrag").resolve()
    kb = create_kb_entry(
        name=name,
        source_dir=str(source),
        data_dir=str(data_dir),
        derived_dir=str(data_dir / "derived"),
        rag_storage=str(data_dir / "rag_storage"),
        port=int(port) if port else next_port(registry, env),
        exclude=[],
    )
    ensure_kb_dirs(kb)
    write_kb_env(kb, env)
    registry["kbs"].append(kb)
    save_registry(registry)
    return kb


# Return True when something accepts TCP connections on the KB's local port.
# Used to refuse a data move while the LightRAG server may have files open.
def _server_is_listening(port: int) -> bool:
    try:
        connection = socket.create_connection(("127.0.0.1", int(port)), timeout=0.5)
    except OSError:
        return False
    connection.close()
    return True


# Compute the sha256 of a file in 1 MiB chunks (never loads a whole file in memory).
def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# Describe a directory tree as {relative path: signature}: regular files by
# (size, sha256) and symlinks by their target, so two trees compare equal only
# when every byte and link survived the copy.
def _tree_signature(root: Path) -> dict:
    signature = {}
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = Path(current) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                signature[rel] = ("link", os.readlink(full))
            elif full.is_file():
                signature[rel] = ("file", full.stat().st_size, _file_sha256(full))
            else:
                signature[rel] = ("dir",)
    return signature


# Verify that dest is an exact copy of src, or raise ValueError.
# Invariant: the old data directory is deleted only after this check passed, so
# a short or corrupted copy can never replace the only good copy. Module-level
# so tests can force a failure.
def _verify_copy(src: Path, dest: Path) -> None:
    source_signature = _tree_signature(Path(src))
    dest_signature = _tree_signature(Path(dest))
    if source_signature != dest_signature:
        missing = sorted(set(source_signature) - set(dest_signature))
        changed = []
        for rel in sorted(set(source_signature) & set(dest_signature)):
            if source_signature[rel] != dest_signature[rel]:
                changed.append(rel)
        raise ValueError(f"copy verification failed (missing: {missing[:5]}, different: {changed[:5]})")


# Choose the source folder that will host the data (<source>/.lightrag).
# With several sources the caller must say which one; guessing could put a
# multi-gigabyte index inside the wrong project.
def _choose_source(kb: dict, source: str | None) -> Path:
    candidates = []
    for raw in [kb["source_dir"]] + list(kb.get("sources", [])):
        resolved = Path(raw).expanduser().resolve()
        if resolved not in candidates:
            candidates.append(resolved)
    listing = ", ".join(str(item) for item in candidates)
    if source is not None:
        wanted = Path(source).expanduser().resolve()
        if wanted not in candidates:
            raise ValueError(f"'{source}' is not a source of KB '{kb['name']}'. Candidates: {listing}")
        return wanted
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"KB '{kb['name']}' has several sources; pass `source` to choose one. Candidates: {listing}")


# Refuse a move whose preconditions are not met; every check raises ValueError.
def _check_move_allowed(kb: dict, current: Path, destination: Path) -> None:
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"Destination is not empty: {destination}")
    if is_relative_to(destination, current) or is_relative_to(current, destination):
        raise ValueError("Destination and current data directory must not contain each other.")
    if _server_is_listening(kb["port"]):
        raise ValueError(f"The server of '{kb['name']}' is running on port {kb['port']}: stop it first.")
    checkpoints = Path(kb["derived_dir"]) / ".checkpoints"
    if checkpoints.is_dir() and list(checkpoints.glob("chk_*.json")):
        raise ValueError("Pending crash-recovery checkpoints exist: run an ingestion pass first.")
    processing = current / "inbox" / "processing"
    if processing.is_dir() and any(processing.iterdir()):
        raise ValueError("The inbox has batches in processing: wait for the worker or run an ingestion pass.")


# Move a KB's data directory between knowledge-os ("repo": kb/<name>/.lightrag) and
# a source folder ("source": <source>/.lightrag). The Catalog is not part of the
# data directory and never moves.
# Safety order: preconditions -> writer lock -> copy -> byte-level verification ->
# atomic rename into place -> Catalog path rebase -> registry save -> only then
# delete the old tree. Any failure before the registry is saved leaves the old
# data and registry untouched and removes the new copy. dry_run runs only the
# checks. Returns a dict describing the move.
def set_data_location(name: str, location: str, source: str | None = None, dry_run: bool = False,
                      env: dict[str, str] | None = None, kb_root: str | Path | None = None) -> dict:
    from pipeline.indexer import KBLockedError, KBWriterLock
    from storage.catalog import Catalog

    if location not in ("repo", "source"):
        raise ValueError(f"location must be 'repo' or 'source', not {location!r}")
    env = env or global_env()
    registry = load_registry(env)
    kb = find_kb(registry, name)
    if kb is None:
        raise ValueError(f"KB '{name}' not found.")

    root = Path(kb_root) if kb_root else KB_ROOT
    current = Path(kb["data_dir"]).resolve()
    if location == "repo":
        destination = (root / name / ".lightrag").resolve()
    else:
        destination = (_choose_source(kb, source) / ".lightrag").resolve()
    result = {"kb": name, "location": location, "from": str(current), "to": str(destination),
              "dry_run": bool(dry_run), "moved": False, "catalog_paths_updated": 0, "warnings": []}
    if current == destination:
        return result
    _check_move_allowed(kb, current, destination)
    if dry_run:
        return result

    old_derived = Path(kb["derived_dir"]).resolve()
    new_derived = destination / "derived"
    staging = destination.parent / (destination.name + ".moving")
    if staging.exists():
        raise ValueError(f"A stale partial copy exists, remove it first: {staging}")

    # Hold the KB writer lock for the whole move so no ingestion pass can write
    # while the tree is being copied.
    lock = KBWriterLock(old_derived / ".kb_writer.lock", kb_id=name)
    try:
        lock.acquire(timeout=0.0)
    except KBLockedError:
        raise ValueError(f"Another writer is active on '{name}'.")
    rebased = 0
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if current.exists():
            shutil.copytree(current, staging, symlinks=True)
            try:
                _verify_copy(current, staging)
            except ValueError:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        else:
            staging.mkdir(parents=True)
        if destination.exists():
            os.rmdir(destination)
        os.replace(staging, destination)
        try:
            with Catalog.open_for_kb(name, root) as catalog:
                rebased = catalog.rebase_artifact_paths(old_derived, new_derived)
            kb["data_dir"] = str(destination)
            kb["derived_dir"] = str(new_derived)
            kb["rag_storage"] = str(destination / "rag_storage")
            ensure_kb_dirs(kb)
            write_kb_env(kb, env)
            save_registry(registry)
        except BaseException:
            # Roll back: the registry still points at the intact old tree.
            if rebased:
                with Catalog.open_for_kb(name, root) as catalog:
                    catalog.rebase_artifact_paths(new_derived, old_derived)
            shutil.rmtree(destination, ignore_errors=True)
            raise
    finally:
        lock.release()

    result["moved"] = True
    result["catalog_paths_updated"] = rebased
    # The registry is saved, so the old tree is no longer referenced.
    try:
        shutil.rmtree(current)
    except OSError as exc:
        result["warnings"].append(f"old data directory could not be removed: {exc}")
    return result
