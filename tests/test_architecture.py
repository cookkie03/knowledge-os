"""Architectural rules: adapters only call tools, and nothing bypasses the tools layer."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
LOCAL_PACKAGES = {"cli", "mcp_server", "tools", "storage", "pipeline", "sync"}


# Return every (module, line) imported by a Python file, including imports inside functions.
def _imports(path: Path) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


# Adapters (CLI, MCP) may import third-party libraries and `tools.*` only, never
# storage, pipeline or each other, so every operation has a single implementation.
def test_adapters_only_import_tools():
    problems = []
    for adapter in ("cli", "mcp_server"):
        for path in sorted((SRC / adapter).glob("*.py")):
            for module, line in _imports(path):
                top = module.split(".")[0]
                if top in LOCAL_PACKAGES and top not in {"tools", adapter}:
                    problems.append(f"{path.name}:{line} imports {module}")
    assert not problems, "adapters must call tools.* only: " + "; ".join(problems)


# The code tree must not depend on sys.path manipulation: entry scripts live in src/.
def test_no_sys_path_hacks_in_source():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if "sys.path" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, "sys.path manipulation found in: " + ", ".join(offenders)


# The tools layer is the lower layer: it never imports an adapter.
def test_tools_do_not_import_adapters():
    problems = []
    for path in sorted((SRC / "tools").rglob("*.py")):
        for module, line in _imports(path):
            if module.split(".")[0] in {"cli", "mcp_server"}:
                problems.append(f"{path.name}:{line} imports {module}")
    assert not problems, "; ".join(problems)
