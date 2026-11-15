from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
KB_ROOT = HOME / "kb"


def remember_memory(
    source_dir: str,
    content: str,
    memory_type: str = "fact",
    title: str = "",
    tags: list[str] | None = None,
    supersedes_id: str | None = None,
    source: str = "agent",
) -> dict:
    """Store an agent memory in Markdown frontmatter format in <source_dir>/memories/."""
    source_path = Path(source_dir).expanduser().resolve()
    memories_dir = source_path / "memories"
    memories_dir.mkdir(parents=True, exist_ok=True)

    # Generate a simple ID based on timestamp
    import time
    mem_id = f"mem_{int(time.time() * 1000)}"

    # Create markdown frontmatter
    tags_str = ", ".join(tags) if tags else ""
    md_content = f"---\n"
    md_content += f"id: {mem_id}\n"
    md_content += f"type: {memory_type}\n"
    md_content += f"title: \"{title}\"\n"
    md_content += f"tags: [{tags_str}]\n"
    md_content += f"source: {source}\n"
    md_content += f"created_at: {os.path.getmtime(source_path) if source_path.exists() else ''}\n"
    md_content += f"updated_at: {int(time.time())}\n"
    md_content += f"---\n\n"
    md_content += content

    mem_path = memories_dir / f"{mem_id}.md"
    mem_path.write_text(md_content, encoding="utf-8")

    return {
        "id": mem_id,
        "type": memory_type,
        "title": title,
        "tags": tags or [],
        "source": source,
        "content": content,
        "file_path": str(mem_path),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def query_memories(
    source_dir: str,
    query: str,
    memory_type: str | None = None,
    active_only: bool = True,
    top_k: int = 5,
) -> list[dict]:
    """Search structured agent memories by query, status, and type."""
    source_path = Path(source_dir).expanduser().resolve()
    memories_dir = source_path / "memories"

    if not memories_dir.is_dir():
        return []

    results: list[dict] = []
    for md_file in memories_dir.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            # Parse frontmatter
            lines = content.split("\n")
            meta = {}
            body_start = 0
            for i, line in enumerate(lines):
                if line.startswith("---"):
                    if i + 1 < len(lines) and not lines[i + 1].startswith("---"):
                        # Parse YAML-like frontmatter
                        for j in range(i + 1, len(lines)):
                            if lines[j].startswith("---"):
                                body_start = j + 1
                                break
                            if ":" in lines[j]:
                                key, _, val = lines[j].partition(":")
                                meta[key.strip()] = val.strip()
                        break

            # Filter by type
            if memory_type and meta.get("type") != memory_type:
                continue

            # Filter by active_only (skip superseded/deprecated)
            if active_only and meta.get("status") in ("superseded", "deprecated"):
                continue

            # Check if query matches content
            if query and query.lower() not in content.lower():
                continue

            results.append({
                "id": meta.get("id", md_file.stem),
                "type": meta.get("type", "fact"),
                "title": meta.get("title", ""),
                "tags": meta.get("tags", "").strip("[]").split(",") if meta.get("tags") else [],
                "source": meta.get("source", "agent"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "content_snippet": content[body_start:body_start + 200] if body_start < len(content) else "",
            })
        except Exception:
            continue

    # Sort by updated_at descending
    results.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return results[:top_k]


def load_kb_memories(source_dir: str) -> list[dict]:
    """Load all memories for a KB."""
    source_path = Path(source_dir).expanduser().resolve()
    memories_dir = source_path / "memories"

    if not memories_dir.is_dir():
        return []

    memories: list[dict] = []
    for md_file in memories_dir.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            lines = content.split("\n")
            meta = {}
            for i, line in enumerate(lines):
                if line.startswith("---"):
                    for j in range(i + 1, len(lines)):
                        if lines[j].startswith("---"):
                            break
                        if ":" in lines[j]:
                            key, _, val = lines[j].partition(":")
                            meta[key.strip()] = val.strip()

            memories.append({
                "id": meta.get("id", md_file.stem),
                "type": meta.get("type", "fact"),
                "title": meta.get("title", ""),
                "tags": meta.get("tags", "").strip("[]").split(",") if meta.get("tags") else [],
                "source": meta.get("source", "agent"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "content": content[len(lines[0]) + 2 if lines[0].startswith("---") else 0:],
            })
        except Exception:
            continue

    return memories


def deprecate_memory_by_id(
    source_dir: str,
    memory_id: str,
    reason: str = "",
    superseded_by: str | None = None,
) -> dict | None:
    """Mark an agent memory as superseded or deprecated."""
    source_path = Path(source_dir).expanduser().resolve()
    memories_dir = source_path / "memories"

    if not memories_dir.is_dir():
        return None

    for md_file in memories_dir.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8")
            if f"id: {memory_id}" in content:
                # Update the memory status
                lines = content.split("\n")
                new_lines = []
                for line in lines:
                    if line.startswith("id: "):
                        new_lines.append(f"id: {memory_id}")
                    elif line.startswith("type: "):
                        new_lines.append(line)
                    elif line.startswith("status: "):
                        new_lines.append(f"status: deprecated")
                    elif line.startswith("reason: "):
                        new_lines.append(f"reason: {reason}")
                    elif line.startswith("supersedes_id: "):
                        new_lines.append(f"supersedes_id: {superseded_by or ''}")
                    else:
                        new_lines.append(line)
                md_file.write_text("\n".join(new_lines), encoding="utf-8")

                # Return updated memory
                updated_meta = {}
                for l in new_lines:
                    if ":" in l:
                        k, _, v = l.partition(":")
                        updated_meta[k.strip()] = v.strip()

                return {
                    "id": updated_meta.get("id", memory_id),
                    "type": updated_meta.get("type", "fact"),
                    "title": updated_meta.get("title", ""),
                    "tags": updated_meta.get("tags", "").strip("[]").split(",") if updated_meta.get("tags") else [],
                    "source": updated_meta.get("source", "agent"),
                    "reason": reason,
                    "superseded_by": superseded_by,
                    "status": "deprecated",
                    "updated_at": int(time.time()),
                }
        except Exception:
            continue

    return None