from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
CONFIG = HOME / "config" / "global.env"
REGISTRY = HOME / "config" / "registry.yaml"


def load_config() -> dict[str, str]:
    """Load the global configuration from global.env."""
    env: dict[str, str] = {}
    if not CONFIG.exists():
        return env
    for line in CONFIG.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def save_config(updates: dict[str, str]) -> None:
    """Atomically update config with the given key-value pairs."""
    lines: list[str] = []
    keys_found: set[str] = set()

    # Read existing config
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
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

    # Add new/update keys
    for k, v in updates.items():
        if k not in keys_found:
            lines.append(f"{k}={v}\n")

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG.parent), prefix=".global_env_")
    try:
        import fcntl
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CONFIG))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_config(key: str, default: str | None = None) -> str | None:
    """Get a single config value."""
    env = load_config()
    return env.get(key, default)


def set_config(key: str, value: str) -> None:
    """Set a single config value atomically."""
    save_config({key: value})


def list_config() -> dict[str, str]:
    """Return all config values."""
    return load_config()