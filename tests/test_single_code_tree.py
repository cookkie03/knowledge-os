from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# bin/ must hold only the shell launcher. Python modules there would form a
# second code tree that silently diverges from src/ (the original T1 bug).
def test_bin_contains_only_the_launcher() -> None:
    names = []
    for entry in (ROOT / "bin").iterdir():
        if entry.name == "__pycache__":
            continue
        names.append(entry.name)
    assert names == ["ragcli"]


# No module or test may import through a "bin" package, because that path
# would resurrect the removed duplicate implementation.
def test_no_imports_from_bin_package() -> None:
    offenders = []
    for folder in (ROOT / "src", ROOT / "tests"):
        for path in folder.rglob("*.py"):
            if path.name == "test_single_code_tree.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "from bin." in text or "from bin import" in text or "import bin." in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
