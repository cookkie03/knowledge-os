from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
DATA_DIR = HOME / "kb"


def _get_kb_data_dir(kb_name: str) -> Path:
    """Get the data directory for a KB."""
    kb_path = DATA_DIR / kb_name
    kb_path.mkdir(parents=True, exist_ok=True)
    return kb_path


def _get_json_path(kb_name: str, identifier: str) -> Path:
    """Get the JSON store path for a KB entry."""
    return _get_kb_data_dir(kb_name) / f"{identifier}.json"


def json_store_set(kb_name: str, identifier: str, data: dict) -> None:
    """Atomically set a value in the JSON store for a KB."""
    path = _get_json_path(kb_name, identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".json_store_")
    try:
        import fcntl
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def json_store_get(kb_name: str, identifier: str) -> dict | None:
    """Get a value from the JSON store for a KB."""
    path = _get_json_path(kb_name, identifier)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def json_store_delete(kb_name: str, identifier: str) -> bool:
    """Delete a value from the JSON store for a KB."""
    path = _get_json_path(kb_name, identifier)
    if path.exists():
        path.unlink()
        return True
    return False


def json_store_list(kb_name: str) -> list[dict]:
    """List all entries in the JSON store for a KB."""
    kb_dir = _get_kb_data_dir(kb_name)
    if not kb_dir.is_dir():
        return []
    results: list[dict] = []
    for f in kb_dir.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                results.append({f.stem: data})
        except (json.JSONDecodeError, OSError):
            continue
    return results