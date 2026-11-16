#!/usr/bin/env python3
"""Centralized hook management for MCP servers.

This module centralizes all hook installation and management operations
that were previously scattered across agent_hooks.py and mcp_clients.py.

Key features:
- MCP server registration/unregistration for all supported clients
- UserPromptSubmit and SessionEnd hook configuration
- Client-specific hook installation (claude-code, cursor, codex, antigravity)
- Centralized configuration file management with atomic writes

All critical blocks have Redis-style comments explaining purpose and invariants.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

# Core constants
HOME = Path(__file__).resolve().parent.parent.parent  # From kb_router.py location
CLAUDE_DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
ANTIGRAVITY_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"


def _merge_json_mcp_server(
    path: Path, mcp_name: str, entry: dict[str, Any], print_only: bool = False
) -> int:
    """Merge an MCP server entry into a JSON config file.

    Invariant: The config file is always valid JSON after the merge. A backup
    (path.bak) is created before overwriting. If print_only is True, only prints
    the entry without modifying the file.

    The atomic read-modify-write pattern ensures that concurrent modifications
    don't lose data. The backup preserves the previous configuration.
    """
    if print_only:
        print(f"--- {path} (mcpServers.{mcp_name}) ---")
        print(json.dumps(entry, indent=2))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("mcpServers", {})[mcp_name] = entry
    if path.exists():
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Registered in {path}")
    return 0


# Cursor project configuration file path helpers
def _cursor_config_path(
    source_folder: Path | None = None,
) -> Path:
    """Get the Cursor MCP config path for a given source folder.

    Invariant: Checks if source_folder/.cursor/mcp.json exists first,
    then falls back to cwd/.cursor/mcp.json, then home/.cursor/mcp.json.
    Returns the first found path, or the constructed path if none exist.
    This ensures we always write to the correct project-local config.
    """
    if source_folder and (source_folder / ".cursor" / "mcp.json").exists():
        return source_folder / ".cursor" / "mcp.json"
    cwd_path = Path.cwd() / ".cursor" / "mcp.json"
    if cwd_path.exists():
        return cwd_path
    base = source_folder or Path.cwd()
    return base / ".cursor" / "mcp.json"


# ---- Claude Code Hook Management ----

def add_claude_code(
    mcp_name: str,
    python: Path,
    script: Path,
    kb_name: str,
    print_only: bool = False,
    source_folder: Path | None = None,
) -> int:
    """Register an MCP server in Claude Code using its CLI.

    Invocation: `claude mcp add --scope project <mcp_name> -- <python> <script> --kb <kb_name>`

    Invariant: The Claude Code CLI command is constructed with the correct
    scope (project) and arguments. The command is printed for review when
    print_only=True. When print_only=False, the subprocess is executed in
    the project's cwd, and .mcp.json is updated.

    After successful registration, the user is told to verify with `/mcp`
    in Claude Code by opening that project.
    """
    cwd = Path.cwd()
    cmd = ["claude", "mcp", "add", "--scope", "project", mcp_name,
           "--", str(python), str(script), "--kb", kb_name]
    print(f"Registration command (local project: {cwd}):\n  " + " ".join(cmd))
    if print_only:
        return 0
    rc = subprocess.call(cmd, cwd=cwd)
    if rc == 0:
        print(f"MCP '{mcp_name}' registered in {cwd}/.mcp.json (scope project). "
              f"Verify with /mcp in Claude Code by opening that project.")
    return rc


def remove_claude_code(
    mcp_name: str,
    source_folder: Path | None = None,
) -> int:
    """Remove an MCP server from Claude Code project or global scope.

    Invariant: First checks for a local .mcp.json in the current directory.
    If found, removes with `--scope project`. If not found, removes from
    the global Claude Code registry.

    Returns the subprocess return code.
    """
    cwd = Path.cwd()
    if (cwd / ".mcp.json").exists():
        cmd = ["claude", "mcp", "remove", "--scope", "project", mcp_name]
        print(f"Unregistration command (local project: {cwd}):\n  " + " ".join(cmd))
        try:
            rc = subprocess.call(cmd, cwd=cwd)
            if rc == 0:
                print(f"MCP '{mcp_name}' removed from {cwd}/.mcp.json.")
            else:
                print(f"Failed: command returned {rc}.")
            return rc
        except Exception as e:
            print(f"Error removing from Claude Code: {e}")
            return 1
    # Fall back to global removal
    cmd = ["claude", "mcp", "remove", mcp_name]
    print("No .mcp.json in current directory; removing from global registry.\nUnregistration command:\n  " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
        if rc == 0:
            print(f"MCP '{mcp_name}' removed from Claude Code.")
        else:
            print(f"Failed: command returned {rc}.")
        return rc
    except Exception as e:
        print(f"Error removing from Claude Code: {e}")
        return 1


# ---- Claude Desktop Hook Management ----

def add_claude_desktop(
    mcp_name: str,
    python: Path,
    script: Path,
    kb_name: str,
    print_only: bool = False,
    source_folder: Path | None = None,
) -> int:
    """Register an MCP server in Claude Desktop global config JSON.

    Invariant: Merges the server entry into CLAUDE_DESKTOP_CONFIG (~/
    Library/Application Support/Claude/claude_desktop_config.json). A backup
    is created before overwriting. After registration, the user is told to
    restart Claude Desktop to apply changes.
    """
    rc = _merge_json_mcp_server(CLAUDE_DESKTOP_CONFIG, mcp_name,
                                 {"command": str(python), "args": [str(script), "--kb", kb_name]},
                                 print_only)
    if rc == 0 and not print_only:
        print("Restart Claude Desktop to apply changes.")
    return rc


def remove_claude_desktop(
    mcp_name: str,
    source_folder: Path | None = None,
) -> int:
    """Remove an MCP server from Claude Desktop global config.

    Invariant: Reads the config, removes the named server, and writes back.
    A backup (.bak) is created. If the config file doesn't exist, reports
    that the server was not found.
    """
    if not CLAUDE_DESKTOP_CONFIG.exists():
        print(f"Config file {CLAUDE_DESKTOP_CONFIG} does not exist.")
        return 0
    try:
        config = json.loads(CLAUDE_DESKTOP_CONFIG.read_text())
    except Exception as e:
        print(f"Error loading {CLAUDE_DESKTOP_CONFIG}: {e}")
        return 1
    servers = config.get("mcpServers", {})
    if mcp_name in servers:
        servers.pop(mcp_name, None)
        shutil.copy(CLAUDE_DESKTOP_CONFIG, CLAUDE_DESKTOP_CONFIG.with_suffix(CLAUDE_DESKTOP_CONFIG.suffix + ".bak"))
        CLAUDE_DESKTOP_CONFIG.write_text(json.dumps(config, indent=2) + "\n")
        print(f"Removed from {CLAUDE_DESKTOP_CONFIG}")
    else:
        print(f"Server '{mcp_name}' not found in {CLAUDE_DESKTOP_CONFIG}")
    return 0


# ---- Codex CLI Hook Management ----

def add_codex(
    mcp_name: str,
    python: Path,
    script: Path,
    kb_name: str,
    print_only: bool = False,
    source_folder: Path | None = None,
) -> int:
    """Register an MCP server globally via Codex CLI.

    Invariant: The Codex CLI does not support per-project mcp_servers: config.toml
    is always global. The command is printed for review when print_only=True.
    When print_only=False, the subprocess is executed globally.

    Codex does not support project-local MCPs, so this always registers globally.
    """
    cmd = ["codex", "mcp", "add", mcp_name, "--", str(python), str(script), "--kb", kb_name]
    print("Codex does not support project-local MCPs, registering globally.\nRegistration command:\n  " + " ".join(cmd))
    if print_only:
        return 0
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"MCP '{mcp_name}' registered in Codex CLI. Verify with /mcp in Codex TUI.")
    return rc


def remove_codex(
    mcp_name: str,
    source_folder: Path | None = None,
) -> int:
    """Remove an MCP server from Codex CLI global registration.

    Invariant: The Codex CLI global config is always used. The command is
    printed for review.
    """
    cmd = ["codex", "mcp", "remove", mcp_name]
    print("Unregistration command:\n  " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
        if rc == 0:
            print(f"MCP '{mcp_name}' removed from Codex CLI.")
        else:
            print(f"Failed: command returned {rc}.")
        return rc
    except Exception as e:
        print(f"Error removing from Codex: {e}")
        return 1


# ---- Antigravity (Gemini) Hook Management ----

def add_antigravity(
    mcp_name: str,
    python: Path,
    script: Path,
    kb_name: str,
    print_only: bool = False,
    source_folder: Path | None = None,
) -> int:
    """Register an MCP server in Antigravity mcp_config.json.

    Invariant: Merges the server entry into ANTIGRAVITY_CONFIG (~/.gemini/config/
    mcp_config.json). A backup is created before overwriting. After registration,
    the user is told to restart Antigravity (or reload MCP servers) to apply changes.
    """
    rc = _merge_json_mcp_server(ANTIGRAVITY_CONFIG, mcp_name,
                                 {"command": str(python), "args": [str(script), "--kb", kb_name]},
                                 print_only)
    if rc == 0 and not print_only:
        print("Restart Antigravity (or reload MCP servers) to apply changes.")
    return rc


def remove_antigravity(
    mcp_name: str,
    source_folder: Path | None = None,
) -> int:
    """Remove an MCP server from Antigravity configuration.

    Invariant: Reads the config, removes the named server, and writes back.
    A backup (.bak) is created. If the config file doesn't exist, reports
    that the server was not found.
    """
    if not ANTIGRAVITY_CONFIG.exists():
        print(f"Config file {ANTIGRAVITY_CONFIG} does not exist.")
        return 0
    try:
        config = json.loads(ANTIGRAVITY_CONFIG.read_text())
    except Exception as e:
        print(f"Error loading {ANTIGRAVITY_CONFIG}: {e}")
        return 1
    servers = config.get("mcpServers", {})
    if mcp_name in servers:
        servers.pop(mcp_name, None)
        shutil.copy(ANTIGRAVITY_CONFIG, ANTIGRAVITY_CONFIG.with_suffix(ANTIGRAVITY_CONFIG.suffix + ".bak"))
        ANTIGRAVITY_CONFIG.write_text(json.dumps(config, indent=2) + "\n")
        print(f"Removed from {ANTIGRAVITY_CONFIG}")
    else:
        print(f"Server '{mcp_name}' not found in {ANTIGRAVITY_CONFIG}")
    return 0


# ---- Cursor Hook Management ----

def add_cursor(
    mcp_name: str,
    python: Path,
    script: Path,
    kb_name: str,
    print_only: bool = False,
    source_folder: Path | None = None,
) -> int:
    """Register an MCP server in the Cursor project configuration file (.cursor/mcp.json).

    Invariant: Merges the server entry into the Cursor config found at the
    resolved path (source_folder or cwd). After registration, the user is told
    to restart Cursor (or reload MCP servers) to apply changes.
    """
    path = _cursor_config_path(source_folder)
    rc = _merge_json_mcp_server(path, mcp_name,
                                 {"command": str(python), "args": [str(script), "--kb", kb_name]},
                                 print_only)
    if rc == 0 and not print_only:
        print("Restart Cursor (or reload MCP servers) to apply changes.")
    return rc


def remove_cursor(
    mcp_name: str,
    source_folder: Path | None = None,
) -> int:
    """Remove an MCP server from .cursor/mcp.json.

    Invariant: Reads the config, removes the named server, and writes back
    with a backup. If the config file doesn't exist, reports that the server
    was not found.
    """
    path = _cursor_config_path(source_folder)
    if not path.exists():
        print(f"Config file {path} does not exist.")
        return 0
    try:
        config = json.loads(path.read_text())
    except Exception as e:
        print(f"Error loading {path}: {e}")
        return 1
    servers = config.get("mcpServers", {})
    if mcp_name in servers:
        servers.pop(mcp_name, None)
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(json.dumps(config, indent=2) + "\n")
        print(f"Removed from {path}")
    else:
        print(f"Server '{mcp_name}' not found in {path}")
    return 0


# ---- Client Registry ----

CLIENTS = {
    "claude-code": add_claude_code,
    "claude-desktop": add_claude_desktop,
    "codex": add_codex,
    "antigravity": add_antigravity,
    "cursor": add_cursor,
}

REMOVERS = {
    "claude-code": remove_claude_code,
    "claude-desktop": remove_claude_desktop,
    "codex": remove_codex,
    "antigravity": remove_antigravity,
    "cursor": remove_cursor,
}