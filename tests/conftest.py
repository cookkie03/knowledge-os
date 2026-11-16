from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "src"
CLI_DIR = SRC_DIR / "cli"
MCP_DIR = SRC_DIR / "mcp"
BIN_DIR = ROOT / "bin"

for path in (SRC_DIR, CLI_DIR, MCP_DIR, ROOT, BIN_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import mcp
    if hasattr(mcp, "__path__") and str(MCP_DIR) not in mcp.__path__:
        mcp.__path__.append(str(MCP_DIR))
except ImportError:
    pass

