#!/usr/bin/env python3
"""Agent hooks for coding agents (Claude Code, Antigravity, Cursor, Codex).

Provides pre-prompt context injection and post-session memory capture,
with client hook configuration management (.claude/settings.json, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.kb import find_kb, load_registry
from tools.memory import query_memories, remember_memory


# Query KB active memories matching the prompt and format an injection context block.
#
# What this does: queries the KB registry for active memories matching the given kb_name and prompt,
# retrieves active preferences first, then query-matched decisions and facts, combines them removing
# duplicates, and formats them into a project_memory XML block for pre-prompt context injection.
# Inputs/Outputs: kb_name (str), prompt (str), max_items (int, default 5) -> str formatted context block
# (empty string if no memories found)
def inject_pre_prompt_context(kb_name: str, prompt: str, max_items: int = 5) -> str:
    reg = load_registry()
    kb_info = find_kb(reg, kb_name)
    if not kb_info:
        return ""

    source_dir = kb_info.get("source_dir")
    if not source_dir:
        return ""

    # Always retrieve active preferences first
    prefs = query_memories(source_dir, query="", memory_type="preference", active_only=True, top_k=3)
    # Then retrieve query-matched decisions and facts
    matched = query_memories(source_dir, query=prompt, active_only=True, top_k=max_items)

    combined = []
    seen_ids = set()
    for m in prefs:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            combined.append(m)
    for m in matched:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            combined.append(m)

    if not combined:
        return ""

    lines = [f'<project_memory kb="{kb_name}">']
    for m in combined:
        mtype = m["type"].upper()
        # Clean one-line summary
        content_preview = m["content"].strip().splitlines()[0][:140]
        title_str = f" ({m["title"]})" if m["title"] and m["title"] not in content_preview else ""
        lines.append(f"- [{mtype}]{title_str}: {content_preview}")
    lines.append("</project_memory>")

    return "\n".join(lines)


# Parse a session transcript or text, extract key decisions or facts, and save episodic/decision memory.
#
# What this does: loads the KB registry and finds the KB info, reads transcript text (from file or
# raw_text parameter), extracts salient lines from the first 5 non-empty lines as a summary preview,
# generates a title from the session ID and preview, and saves the full text as episodic memory
# using the remember_memory function.
# Inputs/Outputs: kb_name (str), transcript_path (optional Path), raw_text (optional str),
# session_id (optional str) -> dict with status, memory_id, and title
def capture_post_session(
    kb_name: str,
    transcript_path: Path | str | None = None,
    raw_text: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    reg = load_registry()
    kb_info = find_kb(reg, kb_name)
    if not kb_info:
        return {"status": "error", "message": f"KB '{kb_name}' not found"}

    source_dir = kb_info.get("source_dir")
    text = raw_text or ""
    if transcript_path:
        t_file = Path(transcript_path)
        if t_file.is_file():
            text = t_file.read_text(encoding="utf-8", errors="replace")

    if not text.strip():
        return {"status": "skipped", "message": "No transcript content provided"}

    # Extract salient lines or summary
    first_lines = [line.strip() for line in text.splitlines() if line.strip()][:5]
    summary_preview = " ".join(first_lines)[:100] or "Session completion"

    sid = session_id or "session"
    title = f"Episodic Log {sid}: {summary_preview}"
    mem = remember_memory(
        source_dir=source_dir,
        content=text.strip(),
        memory_type="episodic",
        title=title,
        tags=["session", "capture"],
        source=f"hook:{sid}",
    )

    return {
        "status": "ok",
        "memory_id": mem["id"],
        "title": mem["title"],
    }


# Configure project-level memory hooks or instructions for supported coding agents.
#
# What this does: configures memory hook instructions for a specified client type (claude-code,
# cursor, codex, or antigravity) in the project directory. For each client, creates the appropriate
# configuration file (settings.json, rules/agent-memory.mdc, instructions.md, or GEMINI.md) with
# memory injection and capture commands. Supports incremental update of existing files.
# Inputs/Outputs: client (str, default "claude-code"), kb_name (str, default "agent-memory"),
# target_project_dir (optional Path) -> dict with status, configured_file, and hooks/info
def install_client_hook(
    client: str = "claude-code",
    kb_name: str = "agent-memory",
    target_project_dir: Path | str | None = None,
) -> dict[str, Any]:
    proj_dir = Path(target_project_dir).resolve() if target_project_dir else Path.cwd().resolve()

    if client == "claude-code":
        claude_dir = proj_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.json"

        settings_data: dict[str, Any] = {}
        if settings_file.is_file():
            try:
                settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                settings_data = {}

        hooks = settings_data.setdefault("hooks", {})
        # Configure UserPromptSubmit hook
        inject_cmd = f"ragcli memory inject --kb {kb_name}"
        capture_cmd = f"ragcli memory capture --kb {kb_name}"

        hooks["UserPromptSubmit"] = [
            {
                "type": "command",
                "command": inject_cmd,
            }
        ]
        hooks["SessionEnd"] = [
            {
                "type": "command",
                "command": capture_cmd,
            }
        ]

        settings_file.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")
        return {
            "status": "ok",
            "client": client,
            "configured_file": str(settings_file),
            "hooks": list(hooks.keys()),
        }

    if client == "cursor":
        rules_dir = proj_dir / ".cursor" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        rules_file = rules_dir / "agent-memory.mdc"
        content = (
            "---\n"
            "description: Agent memory instructions and tools for project context\n"
            "globs: *\n"
            "alwaysApply: true\n"
            "---\n\n"
            f"# Agent Memory Instructions ({kb_name})\n\n"
            f"Use the `{kb_name}` knowledge base to retain context across development sessions.\n\n"
            "## Context Injection\n"
            f"Inject relevant memories before working on tasks:\n"
            f"`ragcli memory inject --kb {kb_name}`\n\n"
            "## Memory Management Tools\n"
            "Use the MCP memory tools to manage long-term project knowledge:\n"
            "- `remember`: Store decisions, architectural choices, and user preferences.\n"
            "- `search_memory`: Search stored project memories and decisions.\n"
            "- `deprecate_memory`: Mark outdated memories as inactive when requirements change.\n"
        )
        rules_file.write_text(content, encoding="utf-8")
        return {
            "status": "ok",
            "client": client,
            "configured_file": str(rules_file),
        }

    if client == "codex":
        codex_dir = proj_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        instructions_file = codex_dir / "instructions.md"
        memory_section = (
            f"# Agent Memory Instructions ({kb_name})\n\n"
            f"Follow these memory guidelines when working on this project:\n"
            f"- Pre-session context injection: `ragcli memory inject --kb {kb_name}`\n"
            f"- Search active memories: `ragcli memory recall --kb {kb_name} \"<query>\"`\n"
            f"- Post-session capture: `ragcli memory capture --kb {kb_name}`\n"
            "- When MCP is available, use `remember`, `search_memory`, and `deprecate_memory` tools.\n"
        )
        if instructions_file.is_file():
            existing = instructions_file.read_text(encoding="utf-8")
            if f"Agent Memory Instructions ({kb_name})" not in existing:
                combined = existing.rstrip() + "\n\n" + memory_section
                instructions_file.write_text(combined, encoding="utf-8")
        else:
            instructions_file.write_text(memory_section, encoding="utf-8")

        return {
            "status": "ok",
            "client": client,
            "configured_file": str(instructions_file),
        }

    if client == "antigravity":
        gemini_dir = proj_dir / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        instructions_file = gemini_dir / "GEMINI.md"
        memory_section = (
            f"# Agent Memory Guidelines ({kb_name})\n\n"
            f"Maintain project context and memory using the `{kb_name}` knowledge base:\n"
            f"- Context recall: `ragcli memory recall --kb {kb_name} \"<query>\"`\n"
            f"- Context injection: `ragcli memory inject --kb {kb_name}`\n"
            f"- Capture session memories: `ragcli memory capture --kb {kb_name}`\n"
            "- Use MCP memory tools `remember`, `search_memory`, and `deprecate_memory` to manage decisions.\n"
        )
        if instructions_file.is_file():
            existing = instructions_file.read_text(encoding="utf-8")
            if f"Agent Memory Guidelines ({kb_name})" not in existing:
                combined = existing.rstrip() + "\n\n" + memory_section
                instructions_file.write_text(combined, encoding="utf-8")
        else:
            instructions_file.write_text(memory_section, encoding="utf-8")

        return {
            "status": "ok",
            "client": client,
            "configured_file": str(instructions_file),
        }

    return {
        "status": "error",
        "message": f"Unsupported client: {client}. Currently supported: claude-code, cursor, codex, antigravity",
    }
