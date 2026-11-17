"""Paths, global configuration and runtime environment (single source of truth).

Every other module resolves the repository root, the config files and the engine
binaries from here, so no module computes its own location from __file__.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from pathlib import Path

# HOME is the repository root: src/tools/system.py -> src/tools -> src -> repo.
HOME = Path(__file__).resolve().parent.parent.parent
CONFIG = HOME / "config" / "global.env"
REGISTRY = HOME / "config" / "registry.yaml"
KB_ROOT = HOME / "kb"
ENGINE_DIR = HOME / ".venv"
ENGINE_PYTHON = ENGINE_DIR / "bin" / "python"
LIGHTRAG_SERVER = ENGINE_DIR / "bin" / "lightrag-server"
MCP_SCRIPT = HOME / "src" / "mcp" / "lightrag_mcp.py"
INGEST_SCRIPT = HOME / "src" / "cli" / "ingest.py"


# Write a text file atomically: lock, temporary file in the same directory, fsync,
# os.replace. A crash or a concurrent writer can therefore never leave a
# half-written file behind; readers see either the old or the new content.
def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name("." + path.name + ".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_path, str(path))
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_env(path: Path) -> dict[str, str]:
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


# Atomically update key-value pairs in global.env using flock locking and atomic os.replace.
# Prevents partial file writes or environment file corruption on process interruption.
def upsert_global_env(updates: dict[str, str], env_path: Path | None = None) -> None:
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
    env = load_env(CONFIG)
    env.setdefault("ENGINE_PYTHON", str(ENGINE_PYTHON))
    env.setdefault("LIGHTRAG_SERVER_BIN", str(LIGHTRAG_SERVER))
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
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False
