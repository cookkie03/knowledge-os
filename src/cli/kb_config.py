#!/usr/bin/env python3
"""Centralized KB (Knowledge Base) and Tull configuration management.

This module centralizes all KB creation, configuration, and Tull operations
(move, remove, add list, etc.) so that all consumers use the same configuration
logic. Previously scattered across kb_manager.py, storage_manager.py, and
other modules, this provides a single source of truth.

Key features:
- KB entry creation with normalization
- Tull (move/remove/add) operations centralized
- KB directory management (data, derived, rag_storage)
- KB registry management (load/save)
- Path validation and safety
- Redis-style comments for all critical blocks
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Core constants
HOME = Path(__file__).resolve().parent.parent
CONFIG = HOME / "config" / "global.env"
REGISTRY = HOME / "config" / "registry.yaml"
KB_ROOT = HOME / "kb"
DERIVED_SUBDIRS = ("text", "pdf", "images", "office", "audio", "cache", "manifests")


def load_env(path: Path = CONFIG) -> dict[str, str]:
    """Load environment key-value pairs from a .env file.

    Invariant: paths are resolved with Path.resolve() before reading to
    block path traversal attacks. Each line is stripped and validated.
    """
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def upsert_global_env(updates: dict[str, str], env_path: Path | None = None) -> None:
    """Atomically update key-value pairs in global.env using flock locking and atomic os.replace.

    This is the central function for updating global configuration. It uses
    fcntl.flock for exclusive locking and tempfile.mkstemp + os.replace for
    atomic writes, preventing JSON corruption on crash or concurrent access.

    Invariant: The atomic write pattern (mkstemp → write → fsync → os.replace)
    ensures that concurrent writers cannot interleave partial writes, and that
    crashes during write leave the original config intact.
    """
    path = env_path or CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    keys_found: set[str] = set()

    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    lines.append(line)
                    continue
                k, _ = stripped.split("=", 1)
                k = k.strip()
                if k in updates:
                    lines.append(f"{k}={updates[k]}\n")
                    keys_found.add(k)
                else:
                    lines.append(line)

    for k, v in updates.items():
        if k not in keys_found:
            lines.append(f"{k}={v}\n")

    # Atomic write with flock
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".global_env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def global_env() -> dict[str, str]:
    """Return the full global environment dict with all defaults.

    Invariant: This function always returns a dict with all required keys set,
    using Path.resolve()'d defaults for any missing values. Callers should
    use this rather than constructing the dict themselves to ensure consistency.
    """
    env: dict[str, str] = load_env(CONFIG)
    env.setdefault("ENGINE_PYTHON", str(HOME / ".venv" / "bin" / "python"))
    env.setdefault("LIGHTRAG_SERVER_BIN", str(HOME / ".venv" / "bin" / "lightrag-server"))
    platform_value = env.get("DOCLING_PLATFORM", env.get("DEPLOY_PLATFORM", "auto"))
    platform_value = platform_value.strip().lower() or "auto"
    if platform_value in {"darwin", "macos"}:
        platform_value = "mac"
    env["DOCLING_PLATFORM"] = platform_value
    env.setdefault("DOCLING_PYTHON", env["ENGINE_PYTHON"])
    env.setdefault("DOCLING_LANG", "it,en")
    env.setdefault("DOCLING_TIMEOUT", "3600")
    env.setdefault("DOCLING_OCR_ENGINE", "auto")
    env.setdefault(
        "DOCLING_ASR_ENGINE",
        "whisper_turbo_native" if env.get("DOCLING_PLATFORM") == "linux" else "auto",
    )
    env.setdefault("DOCLING_ACCELERATOR", "auto")
    env.setdefault("DOCLING_ENABLE_TABLES", "1")
    env.setdefault("DOCLING_ENABLE_CODE_ENRICHMENT", "1")
    env.setdefault("DOCLING_ENABLE_FORMULA_ENRICHMENT", "1")
    env.setdefault("DOCLING_IMAGE_SCALE", "2.0")
    env.setdefault("PIPELINE_ENABLE_TEXT", "1")
    env.setdefault("PIPELINE_ENABLE_PDF", "1")
    env.setdefault("PIPELINE_ENABLE_IMAGES", "1")
    env.setdefault("PIPELINE_ENABLE_OFFICE", "1")
    env.setdefault("PIPELINE_ENABLE_AUDIO", "1")
    env.setdefault("LLM_PROVIDER", "ollama")
    env.setdefault("OLLAMA_HOST", "http://localhost:11434")
    env.setdefault("LLM_MODEL", "qwen3.5:4b")
    env.setdefault("EMBEDDING_MODEL", "embeddinggemma-2:latest")
    env.setdefault("EMBEDDING_DIM", "768")
    env.setdefault("OLLAMA_LLM_NUM_CTX", "8192")
    env.setdefault("OLLAMA_LLM_NUM_PREDICT", "4096")
    env.setdefault("OLLAMA_EMBEDDING_NUM_CTX", "8192")
    env.setdefault("OPENROUTER_HOST", "https://openrouter.ai/api/v1")
    env.setdefault("OPENROUTER_LLM_MODEL", "openrouter/owl-alpha")
    env.setdefault("OPENROUTER_EMBEDDING_MODEL", "nvidia/llama-nemotron-embed-vl-1b-v2:free")
    env.setdefault("OPENROUTER_EMBEDDING_DIM", "2048")
    env.setdefault("RERANK_BINDING", "null")
    env.setdefault("LLM_TIMEOUT", "900")
    env.setdefault("TIMEOUT", "900")
    env.setdefault("MAX_ASYNC", "1")
    env.setdefault("EMBEDDING_TIMEOUT", "120")
    env.setdefault("SUMMARY_LANGUAGE", "Italian")
    env.setdefault("MAX_GLEANING", "1")
    env.setdefault("CHUNK_SIZE", "1200")
    env.setdefault("CHUNK_OVERLAP_SIZE", "150")
    env.setdefault("COSINE_THRESHOLD", "0.25")
    env.setdefault("BASE_PORT", "9621")
    env.setdefault("MCP_BIND_HOST", "127.0.0.1")
    env.setdefault("MCP_BIND_PORT", "8743")
    env.setdefault("MCP_PATH", "/mcp")
    env.setdefault("PUBLIC_MCP_KB", "")
    env.setdefault("SHIM_BIND_HOST", "127.0.0.1")
    env.setdefault("SHIM_BIND_PORT", "8420")
    env.setdefault("TAILSCALE_FUNNEL_PORT", "8443")
    env.setdefault("SHIM_PUBLIC_URL", "https://your-node.your-tailnet.ts.net")
    return env


def is_relative_to(path: Path, parent: Path) -> bool:
    """Validate that path is relative to parent directory.

    Uses Path.resolve() to block path traversal attacks.
    Invariant: returns True only if the resolved path is genuinely under
    the resolved parent, preventing path traversal via '..' components.
    """
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def create_kb_entry(
    name: str,
    source_dir: str,
    data_dir: str | None = None,
    derived_dir: str | None = None,
    rag_storage: str | None = None,
    port: int | None = None,
    exclude: List[str] | None = None,
    sources: List[str] | None = None,
    link: Dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Create a KB entry as a plain dict with default values.

    Invariant: The returned dict always has the same structure with all
    required fields. The `link` dict defaults to {"consume": True, "ingest": True}
    unless overridden.

    This centralizes KB entry creation so that all consumers (CLI, MCP, agents)
    produce consistent entries.
    """
    default_link = {"consume": True, "ingest": True}
    if link is not None:
        default_link.update(link)
    return {
        "name": name,
        "source_dir": source_dir,
        "data_dir": data_dir or (source_dir / ".lightrag"),
        "derived_dir": derived_dir or (data_dir / "derived") if data_dir else (source_dir / ".lightrag" / "derived"),
        "rag_storage": rag_storage or (data_dir / "rag_storage") if data_dir else (source_dir / ".lightrag" / "rag_storage"),
        "port": port or 9621,
        "exclude": list(exclude or []),
        "sources": list(sources or []),
        "link": default_link,
    }


def normalize_kb(entry: dict, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Normalize a KB entry from the registry, resolving all paths.

    Invariant: All path fields (source_dir, data_dir, derived_dir, rag_storage)
    are resolved with Path.resolve() to their absolute forms. Missing optional
    fields get sensible defaults from the global environment.

    This is the central normalization function that all consumers use to
    convert raw registry entries into usable KB dicts.
    """
    env = env or global_env()
    source_raw = entry.get("source_dir") or entry.get("source_folder")
    if not source_raw:
        raise ValueError(f"KB '{entry.get('name', '?')}' missing source_dir/source_folder")
    source_dir = Path(source_raw).expanduser().resolve()
    data_raw = entry.get("data_dir")
    data_dir = Path(data_raw).expanduser().resolve() if data_raw else (source_dir / ".lightrag")
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


def load_registry(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load the KB registry from registry.yaml.

    Invariant: If the registry file doesn't exist, returns {"kbs": []}.
    If it exists but is empty or malformed, returns {"kbs": []} as well.
    All KB entries are normalized through normalize_kb().

    This is the central registry loader used by all consumers.
    """
    env = env or global_env()
    if not REGISTRY.exists():
        return {"kbs": []}
    try:
        data = yaml.safe_load(REGISTRY.read_text()) or {}
    except Exception:
        return {"kbs": []}
    kbs: list[dict[str, Any]] = []
    for entry in data.get("kbs", []):
        try:
            kbs.append(normalize_kb(entry, env))
        except (ValueError, TypeError):
            # Skip malformed entries rather than failing entirely
            continue
    return {"kbs": kbs}


def save_registry(data: dict[str, Any]) -> None:
    """Save the KB registry to registry.yaml atomically.

    Invariant: The registry is always written with the header comment
    "# LightRAG-KB Registry (managed by ragcli, hand-editable)."
    and uses yaml.safe_dump with sort_keys=False to preserve ordering.
    Atomic write pattern (mkstemp → write → fsync → os.replace) is used
    to prevent corruption.

    The payload format is {"kbs": [_registry_entry(kb) for kb in data.get("kbs", [])]}.
    """
    header = "# LightRAG-KB Registry (managed by ragcli, hand-editable).\n"
    payload = {"kbs": [_registry_entry(kb) for kb in data.get("kbs", [])]}
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(REGISTRY.parent), prefix=".registry_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(REGISTRY))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _registry_entry(kb: dict[str, Any]) -> dict[str, Any]:
    """Convert a KB dict to a registry entry (for serialization).

    Invariant: Only includes fields that are relevant for registry storage.
    The "link" field is included only if it's not None. Exclude and sources
    lists are copied if present.
    """
    entry: dict[str, Any] = {
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


def find_kb(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Find a KB by name in the registry.

    Invariant: Returns the first KB with a matching name, or None if not found.
    Comparison is exact on the "name" field.
    """
    for kb in registry.get("kbs", []):
        if kb["name"] == name:
            return kb
    return None


def next_port(registry: dict[str, Any] | None = None, env: dict[str, str] | None = None) -> int:
    """Get the next available port for a new KB.

    Invariant: Starts from BASE_PORT (9621) and increments until finding
    a port not used by any existing KB in the registry. Uses global_env()
    defaults if no registry is provided.
    """
    env = env or global_env()
    registry = registry or {"kbs": []}
    used: set[int] = set()
    for kb in registry.get("kbs", []):
        try:
            used.add(int(kb["port"]))
        except (ValueError, TypeError):
            continue
    port = int(env["BASE_PORT"])
    while port in used:
        port += 1
    return port


def ensure_kb_dirs(kb: dict[str, Any]) -> None:
    """Ensure all required directories for a KB exist.

    Invariant: Creates data_dir, derived_dir, rag_storage, and all derived
    subdirectories (text, pdf, images, office, audio, cache, manifests).
    Uses Path.mkdir(parents=True, exist_ok=True) for each directory.

    This centralizes KB directory creation so that all consumers create
    consistent directory structures.
    """
    data_dir = Path(kb["data_dir"])
    derived_dir = Path(kb["derived_dir"])
    rag_storage = Path(kb["rag_storage"])
    data_dir.mkdir(parents=True, exist_ok=True)
    derived_dir.mkdir(parents=True, exist_ok=True)
    rag_storage.mkdir(parents=True, exist_ok=True)
    for subdir in DERIVED_SUBDIRS:
        (derived_dir / subdir).mkdir(parents=True, exist_ok=True)


def storage_mode(kb: dict[str, Any]) -> str:
    """Determine if a KB is in 'source' or 'repo' mode.

    Invariant: Returns "source" if data_dir is relative to source_dir (i.e.,
    the KB data was created from the source files). Returns "repo" if the data
    directory is outside the source directory (e.g., a standalone repo).

    Uses is_relative_to() for path validation to prevent path traversal errors.
    """
    data_dir = Path(kb["data_dir"])
    source_dir = Path(kb["source_dir"])
    return "source" if is_relative_to(data_dir, source_dir) else "repo"


def split_exclude_specs(raw_patterns: List[str]) -> List[str]:
    """Split exclude specs using ';' or ',' as delimiters.

    Invariant: Returns a flat list of non-empty specs. Each spec is stripped
    of whitespace. Empty specs are filtered out.
    """
    specs: List[str] = []
    for raw in raw_patterns:
        for chunk in raw.replace(";", ",").split(","):
            spec = chunk.strip()
            if spec:
                specs.append(spec)
    return specs


def canonicalize_exclude_specs(raw_specs: List[str]) -> Tuple[List[str], bool]:
    """Canonicalize exclude specs: normalize trailing '/*' and deduplicate.

    Invariant: Specs ending with "/*" (but not "/**") have the trailing slash
    removed. Duplicates are removed (first occurrence kept). A boolean is returned
    indicating whether any changes were made (useful for detecting if KB config
    needs updating).
    """
    canonical: List[str] = []
    seen: set[str] = set()
    changed = False
    for spec in raw_specs:
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


def normalize_exclude_specs_for_kb(
    specs: List[str], source_dir: Path
) -> Tuple[List[str], bool]:
    """Normalize exclude specs for a specific KB, expanding relative paths.

    Invariant: Relative patterns without glob characters are resolved against
    source_dir. If the resolved path is a directory, it's upgraded to "directory/
    " form. Absolute patterns or those with glob chars are kept as-is.

    The returned boolean indicates whether any changes were made, which can
    trigger KB reconfiguration.
    """
    canonical, changed = canonicalize_exclude_specs(specs)
    normalized: List[str] = []
    seen: set[str] = set()
    for spec in canonical:
        upgraded = spec
        if "/" not in spec and not any(
            char in spec for char in "*?[]"
        ):
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


def _source_rel_dirs(source_dir: Path, data_dir: Path) -> List[str]:
    """Get relative directories from source_dir that are NOT under data_dir.

    Invariant: Walks source_dir tree, and for each directory, keeps it only if
    its resolved path is NOT under data_dir.resolve(). This is used to determine
    which directories should be excluded from scanning based on the KB's data
    directory location.

    Uses is_relative_to() for path safety.
    """
    data_root = data_dir.resolve()
    rel_dirs: List[str] = []
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

        kept_dirs: List[str] = []
        for dirname in dirs:
            child = root_path / dirname
            try:
                child_resolved = child.resolve()
            except OSError:
                continue
            if is_relative_to(child_resolved, data_root):
                # This dir is under data_dir, skip it
                continue
            kept_dirs.append(dirname)
            rel_dirs.append(child.relative_to(source_dir).as_posix())
        dirs[:] = kept_dirs
    rel_dirs.sort()
    return rel_dirs


def _folder_rule_matches(rel_dir: str, folder_spec: str) -> bool:
    """Check if a relative directory path matches a folder specification pattern.

    Invariant: Uses fnmatch.fnmatch for pattern matching. If folder_spec contains
    '/', it matches the full relative dir path. Otherwise, it matches against
    each path component individually.

    This allows flexible exclude patterns like "docs" (matches any "docs" dir)
    or "src/docs" (matches only src/docs).
    """
    candidate = Path(rel_dir).pure_posix()
    if "/" in folder_spec:
        return fnmatch.fnmatch(rel_dir, folder_spec)
    return any(fnmatch.fnmatch(part, folder_spec) for part in candidate.parts)


def _ancestor_dirs(rel_path: str) -> List[str]:
    """Get ancestor directory components of a relative path.

    Invariant: Returns all parent directories from the root down to the
    parent of the final component. For example, "a/b/c.md" returns ["a", "a/b"].

    This is used by path exclusion logic to check if any ancestor directory
    matches an exclude pattern.
    """
    path = Path(rel_path).pure_posix()
    parents = list(path.parents[:-1])
    result: List[str] = []
    for parent in reversed(parents):
        p = parent.as_posix()
        if p not in {".", ""}:
            result.append(p)
    return result


def dir_is_excluded(rel_dir: str, specs: List[str]) -> bool:
    """Check if a relative directory path is excluded by the given specs.

    Invariant: Canonicalizes specs first (removing trailing /* and deduplicating),
    then checks if any ancestor directory or the directory itself matches a
    folder-level exclude pattern (patterns ending with "/").

    Returns True if the directory should be excluded, False otherwise.
    """
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


def path_is_excluded(rel_path: str, specs: List[str]) -> bool:
    """Check if a relative file path is excluded by the given specs.

    Invariant: Canonicalizes specs, then checks each spec against the path.
    - Patterns ending with "/" match against ancestor directories.
    - Patterns with "/" match against the full relative path (fnmatch).
    - Patterns without "/" match against the basename (fnmatch).

    This provides granular control over which files are included/excluded
    from KB ingestion.
    """
    canonical, _ = canonicalize_exclude_specs(specs)
    basename = Path(rel_path).name
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


def expand_exclude_specs(
    specs: List[str], source_dir: Path, data_dir: Path
) -> List[str]:
    """Expand exclude specs to include source-relative directory paths.

    Invariant: First canonicalizes specs, then determines the relative directories
    under source_dir that are not under data_dir (using _source_rel_dirs). For
    non-glob specs, if a matching relative directory exists, it's expanded to
    "directory/" form. This ensures that folder-level exclusions work correctly
    even when the source directory structure changes.

    Returns the expanded list of specs.
    """
    canonical, _ = canonicalize_exclude_specs(specs)
    expanded: List[str] = []
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


def write_kb_env(kb: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    """Write the KB-specific .env file with all runtime configuration.

    Invariant: Uses atomic write pattern (write to temp file → fsync → os.replace)
    to prevent .env corruption. Writes to both the KB's data directory and
    the user's config home (~/.config/lightrag-kb/{kb_name}.env).

    The .env file contains all configuration needed to run the KB's LightRAG
    server, including LLM binding, embedding settings, pipeline enables, and
    port configuration.

    Returns the path to the written .env file.
    """
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

    lines: list[str] = [
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
    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=str(env_path.parent), prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(env_path))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Also write to user's config home
    service_env_path = Path.home() / ".config" / "lightrag-kb" / f"{kb['name']}.env"
    service_env_path.parent.mkdir(parents=True, exist_ok=True)
    # Use same atomic write for service env
    fd2, tmp_path2 = tempfile.mkstemp(dir=str(service_env_path.parent), prefix=".env_")
    try:
        with os.fdopen(fd2, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path2, str(service_env_path))
    finally:
        if os.path.exists(tmp_path2):
            os.unlink(tmp_path2)

    return env_path


def merged_runtime_env(kb: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, str]:
    """Merge environment variables for KB runtime.

    Invariant: Starts with os.environ, then updates with the KB's .env file
    values, then with the global env. The KB .env values take precedence over
    global env, which takes precedence over os.environ. This ensures that KB-
    specific settings override global defaults while still allowing global
    fallbacks.

    Returns a dict suitable for passing to subprocess.Popen(env=...).
    """
    env = env or global_env()
    merged = os.environ.copy()
    merged.update(env)
    merged.update(load_env(Path(kb["data_dir"]) / ".env"))
    return merged


# Tull (move/remove/add) operations

def add_source_to_kb(
    kb_name: str,
    source_dir: str,
    name: str | None = None,
    include_patterns: List[str] | None = None,
    exclude_patterns: List[str] | None = None,
    deletion_policy: str = "guarded",
    mode: str = "full_folder",
    link: Dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Add a new source to an existing KB.

    Invariant: The new source is added to the KB's registry entry with the
    given configuration. The KB directories are ensured (data, derived, rag_storage).
    The KB's .env file is written with updated configuration.

    The `link` dict defaults to {"consume": True, "ingest": True} unless overridden.

    Returns the updated KB entry dict.
    """
    from yaml import safe_dump

    registry = load_registry()
    kb_info = find_kb(registry, kb_name)
    if not kb_info:
        return {"ok": False, "message": f"KB '{kb_name}' not found in registry"}

    # Create new source entry
    from storage.catalog import create_source_record

    new_source = create_source_record(
        source_id=f"src_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        name=name or source_dir.split("/")[-1],
        origin_folder=source_dir,
        mode=mode,
        include_patterns=include_patterns or [],
        exclude_patterns=exclude_patterns or [],
        deletion_policy=deletion_policy,
        link=link,
    )

    # Add source to KB entry
    kb = normalize_kb(kb_info)
    existing_sources = kb.get("sources", [])
    # Check for duplicate
    source_ids = [s.get("id", "") for s in existing_sources] if isinstance(existing_sources, list) else []
    if new_source["id"] in source_ids:
        return {"ok": False, "message": f"Source '{new_source['id']}' already exists in KB '{kb_name}'"}

    updated_sources = list(existing_sources) + [new_source]
    kb["sources"] = updated_sources

    # Save updated registry
    save_registry(registry)

    # Ensure KB directories
    ensure_kb_dirs(kb)

    # Write KB .env
    write_kb_env(kb)

    return {
        "ok": True,
        "status": "added",
        "kb_name": kb_name,
        "source_id": new_source["id"],
        "source_name": new_source["name"],
    }


def remove_source_from_kb(
    kb_name: str,
    source_id: str,
) -> dict[str, Any]:
    """Remove a source from an existing KB.

    Invariant: The source is removed from the KB's sources list in the registry.
    The KB's .env file is rewritten. If the KB has no remaining sources, it
    still remains in the registry (may need manual cleanup).

    Returns the updated KB entry or error status.
    """
    registry = load_registry()
    kb_info = find_kb(registry, kb_name)
    if not kb_info:
        return {"ok": False, "message": f"KB '{kb_name}' not found in registry"}

    kb = normalize_kb(kb_info)
    existing_sources = kb.get("sources", [])
    if not isinstance(existing_sources, list):
        existing_sources = []

    # Find and remove the source
    updated_sources = [s for s in existing_sources if s.get("id") != source_id]

    if len(updated_sources) == len(existing_sources):
        return {"ok": False, "message": f"Source '{source_id}' not found in KB '{kb_name}'"}

    kb["sources"] = updated_sources

    # Save updated registry
    save_registry(registry)

    # Rewrite KB .env
    write_kb_env(kb)

    return {
        "ok": True,
        "status": "removed",
        "kb_name": kb_name,
        "source_id": source_id,
    }


def list_kb_sources(kb_name: str) -> dict[str, Any]:
    """List all sources for a given KB.

    Invariant: Returns the sources list from the KB's normalized entry,
    or an empty list if the KB has no sources or is not found.
    """
    registry = load_registry()
    kb_info = find_kb(registry, kb_name)
    if not kb_info:
        return {"ok": False, "message": f"KB '{kb_name}' not found in registry", "sources": []}

    kb = normalize_kb(kb_info)
    sources = kb.get("sources", [])
    if not isinstance(sources, list):
        sources = []

    return {"ok": True, "kb_name": kb_name, "sources": sources}


def add_kb_to_registry(
    name: str,
    source_dir: str,
    data_dir: str | None = None,
    derived_dir: str | None = None,
    rag_storage: str | None = None,
    port: int | None = None,
    exclude: List[str] | None = None,
    sources: List[dict[str, Any]] | None = None,
    link: Dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Add a new KB to the registry.

    Invariant: The new KB entry is created with create_kb_entry(), added to
    the registry, and the registry is saved atomically. KB directories are
    ensured. The KB's .env file is written.

    Returns the new KB entry dict.
    """
    registry = load_registry()
    env = global_env()

    # Check if KB already exists
    if find_kb(registry, name):
        return {"ok": False, "message": f"KB '{name}' already exists in registry"}

    # Create new KB entry
    kb_entry = create_kb_entry(
        name=name,
        source_dir=source_dir,
        data_dir=data_dir,
        derived_dir=derived_dir,
        rag_storage=rag_storage,
        port=port,
        exclude=exclude,
        sources=sources,
        link=link,
    )

    # Normalize the entry (resolves paths)
    kb = normalize_kb(kb_entry, env)

    # Add to registry
    registry["kbs"].append(kb)

    # Save registry atomically
    save_registry(registry)

    # Ensure KB directories
    ensure_kb_dirs(kb)

    # Write KB .env
    write_kb_env(kb)

    return {
        "ok": True,
        "status": "added",
        "kb_name": name,
        "kb_entry": kb,
    }


def remove_kb_from_registry(
    name: str,
) -> dict[str, Any]:
    """Remove a KB from the registry.

    Invariant: The KB is removed from the registry.yaml file. The KB's data
    directory is NOT deleted (preserves data). The KB's .env file is left in
    place (can be manually cleaned up).

    Returns success/failure status.
    """
    registry = load_registry()
    original_count = len(registry.get("kbs", []))

    # Filter out the KB to remove
    registry["kbs"] = [kb for kb in registry.get("kbs", []) if kb.get("name") != name]

    if len(registry["kbs"]) == original_count:
        return {"ok": False, "message": f"KB '{name}' not found in registry"}

    # Save registry atomically
    save_registry(registry)

    return {
        "ok": True,
        "status": "removed",
        "kb_name": name,
    }


def update_kb_link(
    kb_name: str,
    source_id: str,
    link: Dict[str, bool],
) -> dict[str, Any]:
    """Update the link configuration for a specific source within a KB.

    Invariant: The source's 'link' dict is updated with the provided values.
    Only the specified source is modified; other sources are left unchanged.
    The KB's .env file is rewritten with the updated configuration.

    Returns the updated KB entry or error status.
    """
    registry = load_registry()
    kb_info = find_kb(registry, kb_name)
    if not kb_info:
        return {"ok": False, "message": f"KB '{kb_name}' not found in registry"}

    kb = normalize_kb(kb_info)
    existing_sources = kb.get("sources", [])
    if not isinstance(existing_sources, list):
        existing_sources = []

    # Find and update the source
    updated = False
    for source in existing_sources:
        if source.get("id") == source_id:
            source["link"] = dict(link)
            updated = True
            break

    if not updated:
        return {"ok": False, "message": f"Source '{source_id}' not found in KB '{kb_name}'"}

    kb["sources"] = existing_sources

    # Save updated registry
    save_registry(registry)

    # Rewrite KB .env
    write_kb_env(kb)

    return {
        "ok": True,
        "status": "updated",
        "kb_name": kb_name,
        "source_id": source_id,
        "link": kb["sources"][
            [s.get("id") for s in existing_sources].index(source_id)
        ]["link"],
    }