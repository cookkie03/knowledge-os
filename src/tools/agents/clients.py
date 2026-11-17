"""Client integrations for `ragcli mcp-add` — registers KB MCP in third-party tools.

Supported clients: claude-code, claude-desktop, codex, antigravity, cursor.
Each uses a different mechanism: the two with dedicated CLIs (claude-code, codex)
invoke system commands; the others directly edit their
JSON config file (mcpServers), with a .bak backup of the previous version.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

CLAUDE_DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
ANTIGRAVITY_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"

# Dispatch table mapping client names to their registration functions.
# Invariant: every key here must also have an entry in REMOVERS so that
# `ragcli mcp add --client all` and `ragcli mcp rm --client all` operate symmetrically.
#
# What this does: maps each client type (claude-code, claude-desktop, codex, antigravity, cursor)
# to its corresponding add/remove registration function.
# Inputs/Outputs: dict keyed by client name, values are registration/removal functions.
CLIENTS: dict  # defined after all add_* functions


# Construct the standard MCP command and argument dictionary for the lightrag server.
#
# What this does: builds a dict with 'command' (python executable path) and 'args'
# (script path + kb name flag) for use across all MCP client registration functions.
# Inputs/Outputs: python (Path to Python executable), script (Path to MCP script),
# kb_name (str knowledge base name) -> dict {"command": str, "args": [str, str, str]}
def _server_args(script: Path, kb_name: str | None) -> list[str]:
    # The router serves every KB, so --kb is added only for a legacy single-KB registration.
    return [str(script)] + (["--kb", kb_name] if kb_name else [])


def _server_entry(python: Path, script: Path, kb_name: str | None) -> dict:
    return {"command": str(python), "args": _server_args(script, kb_name)}


# Merge one server without discarding other settings; back up before overwriting.
#
# What this does: merges a single MCP server entry into a JSON config file, creating a
# backup of the previous version before overwriting. Handles both the case where the config
# file exists and where it needs to be created.
# Inputs/Outputs: path (Path to config file), mcp_name (str server name), entry (dict server
# configuration), print_only (bool if True only prints without modifying) -> int return code (0 success)
def _merge_json_mcp_server(path: Path, mcp_name: str, entry: dict, print_only: bool) -> int:
    if print_only:
        print(f"--- {path} (mcpServers.{mcp_name}) ---")
        print(json.dumps(entry, indent=2))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("mcpServers", {})[mcp_name] = entry
    if path.exists():
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Registered in {path}")
    return 0


# Resolve the Cursor project configuration file path (.cursor/mcp.json).
#
# What this does: finds the .cursor/mcp.json config file, checking source_folder first, then
# cwd, then falling back to the base directory. Supports per-project and global cursor config.
# Inputs/Outputs: source_folder (optional Path to check first) -> Path to the .cursor/mcp.json file
def _cursor_config_path(source_folder: Path | None = None) -> Path:
    if source_folder and (source_folder / ".cursor" / "mcp.json").exists():
        return source_folder / ".cursor" / "mcp.json"
    cwd_path = Path.cwd() / ".cursor" / "mcp.json"
    if cwd_path.exists():
        return cwd_path
    base = source_folder or Path.cwd()
    return base / ".cursor" / "mcp.json"


# Register the project MCP server in Claude Code using its CLI.
#
# What this does: constructs and prints the claude mcp add command for project-scoped MCP
# server registration. The command includes the MCP name, python path, script path, and kb_name.
# If print_only is True, only prints the command without executing it. Otherwise, executes via
# subprocess and confirms registration.
# Inputs/Outputs: mcp_name (str), python (Path), script (Path), kb_name (str), print_only (bool),
# source_folder (optional Path) -> int return code (0 success)
def add_claude_code(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
                     source_folder: Path | None = None) -> int:
    cwd = Path.cwd()
    cmd = ["claude", "mcp", "add", "--scope", "project", mcp_name,
           "--", str(python)] + _server_args(script, kb_name)
    print(f"Registration command (local project: {cwd}):\n  " + " ".join(cmd))
    if print_only:
        return 0
    rc = subprocess.call(cmd, cwd=cwd)
    if rc == 0:
        print(f"MCP '{mcp_name}' registered in {cwd}/.mcp.json (scope project). "
              f"Verify with /mcp in Claude Code by opening that project.")
    return rc


# Register the server into Claude Desktop global config JSON.
#
# What this does: merges the MCP server entry into the Claude Desktop global config JSON file
# (claude_desktop_config.json). Backs up the existing config before overwriting. Prints a reminder
# to restart Claude Desktop to apply changes.
# Inputs/Outputs: mcp_name (str), python (Path), script (Path), kb_name (str), print_only (bool),
# source_folder (optional Path) -> int return code (0 success)
def add_claude_desktop(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
                        source_folder: Path | None = None) -> int:
    rc = _merge_json_mcp_server(CLAUDE_DESKTOP_CONFIG, mcp_name, _server_entry(python, script, kb_name), print_only)
    if rc == 0 and not print_only:
        print("Restart Claude Desktop to apply changes.")
    return rc


# Register the server globally via Codex CLI.
#
# What this does: constructs and prints the codex mcp add command for global MCP registration.
# Codex does not support per-project mcp_servers: config.toml is always global. Prints the command
# and executes it via subprocess if print_only is False.
# Inputs/Outputs: mcp_name (str), python (Path), script (Path), kb_name (str), print_only (bool),
# source_folder (optional Path) -> int return code (0 success)
def add_codex(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
              source_folder: Path | None = None) -> int:
    # codex CLI does not support per-project mcp_servers: config.toml is always global.
    cmd = ["codex", "mcp", "add", mcp_name, "--", str(python)] + _server_args(script, kb_name)
    print("Codex does not support project-local MCPs, registering globally.\nRegistration command:\n  " + " ".join(cmd))
    if print_only:
        return 0
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"MCP '{mcp_name}' registered in Codex CLI. Verify with /mcp in Codex TUI.")
    return rc


# Register the server into Antigravity mcp_config.json.
#
# What this does: merges the MCP server entry into the Antigravity global config JSON file
# (.gemini/config/mcp_config.json). Backs up the existing config before overwriting. Prints a
# reminder to restart Antigravity to apply changes.
# Inputs/Outputs: mcp_name (str), python (Path), script (Path), kb_name (str), print_only (bool),
# source_folder (optional Path) -> int return code (0 success)
def add_antigravity(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
                     source_folder: Path | None = None) -> int:
    rc = _merge_json_mcp_server(ANTIGRAVITY_CONFIG, mcp_name, _server_entry(python, script, kb_name), print_only)
    if rc == 0 and not print_only:
        print("Restart Antigravity (or reload MCP servers) to apply changes.")
    return rc


# Register the KB MCP server in the Cursor project configuration file (.cursor/mcp.json).
#
# What this does: finds the .cursor/mcp.json config file (checking source_folder, cwd, or base)
# and merges the MCP server entry into it, creating a backup of the previous version. Prints a
# reminder to restart Cursor to apply changes.
# Inputs/Outputs: mcp_name (str), python (Path), script (Path), kb_name (str), print_only (bool),
# source_folder (optional Path) -> int return code (0 success)
def add_cursor(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
               source_folder: Path | None = None) -> int:
    path = _cursor_config_path(source_folder)
    rc = _merge_json_mcp_server(path, mcp_name, _server_entry(python, script, kb_name), print_only)
    if rc == 0 and not print_only:
        print("Restart Cursor (or reload MCP servers) to apply changes.")
    return rc


CLIENTS = {
    "claude-code": add_claude_code,
    "claude-desktop": add_claude_desktop,
    "codex": add_codex,
    "antigravity": add_antigravity,
    "cursor": add_cursor,
}


# Remove a JSON-configured server only after successfully reading its config.
#
# What this does: removes a JSON-configured MCP server from a config file after reading it.
# Creates a .bak backup of the original config before writing the modified version. Reads the
# config, pops the mcp_name from mcpServers, and writes the updated config back.
# Inputs/Outputs: path (Path to config file), mcp_name (str server name) -> int return code (0 success)
def _remove_json_mcp_server(path: Path, mcp_name: str) -> int:
    if not path.exists():
        print(f"File {path} does not exist.")
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


# Remove the server registration from Claude Code project or global scope.
#
# What this does: removes an MCP server registration from Claude Code. Checks for a local
# .mcp.json first; if it exists, uses --scope project to remove from project scope. Otherwise
# removes from the global registry. Executes via claude mcp remove subprocess.
# Inputs/Outputs: mcp_name (str), source_folder (optional Path) -> int return code (0 success)
def remove_claude_code(mcp_name: str, source_folder: Path | None = None) -> int:
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


# Remove the server from Claude Desktop global config.
#
# What this does: removes an MCP server from the Claude Desktop global config JSON file
# (claude_desktop_config.json) by calling _remove_json_mcp_server with the configured path.
# Inputs/Outputs: mcp_name (str), source_folder (optional Path) -> int return code (0 success)
def remove_claude_desktop(mcp_name: str, source_folder: Path | None = None) -> int:
    return _remove_json_mcp_server(CLAUDE_DESKTOP_CONFIG, mcp_name)


# Remove the server from Codex CLI global registration.
#
# What this does: removes an MCP server from the Codex CLI global registration by executing
# the codex mcp remove command. Prints the unregistration command and executes it via subprocess.
# Inputs/Outputs: mcp_name (str), source_folder (optional Path) -> int return code (0 success)
def remove_codex(mcp_name: str, source_folder: Path | None = None) -> int:
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


# Remove the server from Antigravity configuration.
#
# What this does: removes an MCP server from the Antigravity global config (.gemini/config/mcp_config.json)
# by calling _remove_json_mcp_server with the configured path.
# Inputs/Outputs: mcp_name (str), source_folder (optional Path) -> int return code (0 success)
def remove_antigravity(mcp_name: str, source_folder: Path | None = None) -> int:
    return _remove_json_mcp_server(ANTIGRAVITY_CONFIG, mcp_name)


# Remove the specified server from .cursor/mcp.json and create a backup of the original config.
#
# What this does: finds the .cursor/mcp.json config file (checking source_folder, cwd, or base)
# and removes the specified MCP server from it, creating a .bak backup of the original config.
# Calls _remove_json_mcp_server with the resolved path.
# Inputs/Outputs: mcp_name (str), source_folder (optional Path) -> int return code (0 success)
def remove_cursor(mcp_name: str, source_folder: Path | None = None) -> int:
    path = _cursor_config_path(source_folder)
    return _remove_json_mcp_server(path, mcp_name)


REMOVERS = {
    "claude-code": remove_claude_code,
    "claude-desktop": remove_claude_desktop,
    "codex": remove_codex,
    "antigravity": remove_antigravity,
    "cursor": remove_cursor,
}
