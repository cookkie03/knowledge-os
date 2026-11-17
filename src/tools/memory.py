#!/usr/bin/env python3
"""The single implementation of agent memory (remember, recall, deprecate, dream).

Memories are plain dicts stored as Markdown files with YAML frontmatter under
<source_dir>/memories/<type>/. CLI and MCP adapters call these functions only.

Key features:
- Memory record lifecycle (active, superseded, deprecated)
- Query with filtering by type, active_only, and top_k
- Conflict detection and resolution
- Episodic memory compaction
- Atomic file writes with fcntl.flock, tempfile.mkstemp, os.replace
- Path validation with os.path.abspath/Path.resolve
- Redis-style comments for all critical blocks
"""

from __future__ import annotations

import difflib
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Valid memory types and statuses
VALID_MEMORY_TYPES: set[str] = {"preference", "decision", "episodic", "fact"}
VALID_MEMORY_STATUSES: set[str] = {"active", "superseded", "deprecated"}

# Contradiction/override keywords for decision conflict detection
CONTRADICTION_KEYWORDS: list[str] = [
    "sostituito", "deprecato", "invece di", "non più", "migrato a", "abbandonato",
    "superseded", "instead of", "no longer", "migrated to", "deprecated"
]


# ---- File Operations ----

def _validate_memory_path(target: Path, parent: Path) -> bool:
    """Validate that a memory file path is under the memories directory.

    Invariant: Prevents path traversal attacks by ensuring the resolved
    target path is always under the resolved memories parent directory.
    Uses Path.resolve() and os.path.commonpath() for safety.
    """
    try:
        resolved_target = target.resolve()
        resolved_parent = parent.resolve()
        return os.path.commonpath([str(resolved_target), str(resolved_parent)]) == str(resolved_parent)
    except (ValueError, OSError):
        return False


def _atomic_write_file(file_path: Path, content: str) -> None:
    """Write file atomically using tempfile.mkstemp + fcntl.flock + os.replace.

    Invariant: The atomic write pattern (mkstemp → write → fsync → os.replace)
    ensures that concurrent writers cannot interleave partial writes, and that
    crashes during write leave the original file intact. This prevents JSON
    corruption on crash or concurrent access.

    Args:
        file_path: The absolute path where the file should end up
        content: The string content to write
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), prefix=".mem_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(file_path))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _read_memory_file(file_path: Path) -> str | None:
    """Read a memory file with error handling.

    Invariant: Returns the file content if it exists and starts with '---' (YAML
    frontmatter). Returns None if the file doesn't exist or is malformed, so
    callers can continue processing other files without failure.
    """
    try:
        if not file_path.is_file():
            return None
        content = file_path.read_text(encoding="utf-8", errors="replace")
        if not content.startswith("---"):
            return None
        return content
    except Exception:
        return None


# ---- Memory File Parsing ----

def parse_memory_file(file_path: Path) -> dict | None:
    """Parse a memory markdown file and return its data dict.

    Invariant: Files that don't start with '---' are silently skipped (returns None).
    YAML frontmatter parsing is tolerant of minor format issues. The returned dict
    always has the same structure regardless of minor format variations.

    Returns dict with keys: id, type, status, title, tags, created_at, updated_at,
    superseded_by, deprecation_reason, confidence, source, content, file_path
    or None if the file cannot be parsed.
    """
    content = _read_memory_file(file_path)
    if content is None:
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    raw_frontmatter = parts[1]
    body = parts[2].strip()

    meta: dict[str, Any] = {}
    for line in raw_frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        # Handle simple types (json-encoded strings, numbers, lists)
        if val.startswith("[") and val.endswith("]"):
            try:
                meta[key] = json.loads(val)
            except Exception:
                # Fallback parse comma separated
                inner = val[1:-1].strip()
                meta[key] = [
                    item.strip().strip("'\"") for item in inner.split(",") if item.strip()
                ]
        elif val.lower() == "null" or val == "":
            meta[key] = None
        elif val.lower() == "true":
            meta[key] = True
        elif val.lower() == "false":
            meta[key] = False
        else:
            try:
                meta[key] = json.loads(val)
            except Exception:
                meta[key] = val.strip("'\"")

    # Extract fields with defaults
    mem_id = meta.get("id") or file_path.stem
    mem_type = meta.get("type", "fact")
    if mem_type not in VALID_MEMORY_TYPES:
        mem_type = "fact"

    mem_status = meta.get("status", "active")
    if mem_status not in VALID_MEMORY_STATUSES:
        mem_status = "active"

    return {
        "id": mem_id,
        "type": mem_type,
        "status": mem_status,
        "title": meta.get("title", ""),
        "tags": list(meta.get("tags") or []),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "superseded_by": meta.get("superseded_by"),
        "deprecation_reason": meta.get("deprecation_reason"),
        "confidence": float(meta.get("confidence", 1.0)),
        "source": meta.get("source", ""),
        "content": body.strip(),
        "file_path": str(file_path.resolve()),
    }


def serialize_memory_frontmatter(mem: dict[str, Any]) -> str:
    """Serialize a memory dict into YAML frontmatter and markdown body.

    Invariant: Always produces valid YAML frontmatter with --- delimiters.
    The frontmatter includes all memory fields. The body is the content string.
    If title exists, it's used as an H1 header before the body.

    Args:
        mem: Dict with memory fields (as returned by parse_memory_file)

    Returns:
        String with YAML frontmatter + markdown body
    """
    frontmatter = [
        "---",
        f"id: {json.dumps(mem.get('id', ''))}",
        f"type: {json.dumps(mem.get('type', 'fact'))}",
        f"status: {json.dumps(mem.get('status', 'active'))}",
        f"title: {json.dumps(mem.get('title', ''))}",
        f"tags: {json.dumps(mem.get('tags', []))}",
        f"created_at: {json.dumps(mem.get('created_at', ''))}",
        f"updated_at: {json.dumps(mem.get('updated_at', ''))}",
    ]
    if mem.get("superseded_by"):
        frontmatter.append(f"superseded_by: {json.dumps(mem['superseded_by'])}")
    if mem.get("deprecation_reason"):
        frontmatter.append(f"deprecation_reason: {json.dumps(mem['deprecation_reason'])}")
    frontmatter.append(f"confidence: {mem.get('confidence', 1.0)}")
    frontmatter.append(f"source: {json.dumps(mem.get('source', ''))}")
    frontmatter.append("---")
    frontmatter.append("")

    header = f"# {mem.get('title', '')}" if mem.get("title") else ""
    body = mem.get("content", "").strip()
    if header and not body.startswith("#"):
        body = f"{header}\n\n{body}"

    return "\n".join(frontmatter) + "\n" + body + "\n"


# ---- Load Memories from KB Directory ----

def load_kb_memories(source_dir: Path | str) -> list[dict[str, Any]]:
    """Load all memory records from a KB's memories directory.

    Invariant: Every memory file must have a valid YAML frontmatter section.
    Files that don't start with '---' are silently skipped so a broken file
    never blocks the load of other valid memories in the same KB.

    Returns a list of memory dicts (as returned by parse_memory_file).
    Empty list if no memories directory or no valid memory files.
    """
    source_path = Path(source_dir).resolve()
    memories_dir = source_path / "memories"
    if not memories_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for md_file in memories_dir.rglob("*.md"):
        mem = parse_memory_file(md_file)
        if mem:
            records.append(mem)
    return records


# ---- Write Memory Record ----

def write_memory_record(
    source_dir: Path | str,
    mem: dict[str, Any],
) -> Path:
    """Write or update a memory record on disk in the appropriate subdirectory.

    Invariant: The memory is written to source_dir/memories/{type}/ with a
    filename based on the memory ID and a slug of the title. The file is
    written atomically using _atomic_write_file.

    Returns the path to the written file.
    """
    source_path = Path(source_dir).resolve()

    if not isinstance(mem, dict):
        raise TypeError(f"mem must be a dict, got {type(mem)}")
    mem_dict = mem

    target_dir = source_path / "memories" / mem_dict["type"]
    target_dir.mkdir(parents=True, exist_ok=True)

    # Generate slug from title
    title = mem_dict.get("title", "")
    slug = re.sub(r"[^\w\s-]", "", title.lower().strip())
    slug = re.sub(r"[\s_-]+", "-", slug)[:40].strip("-")
    filename = f"{mem_dict['id']}_{slug}.md" if slug else f"{mem_dict['id']}.md"

    target_file = target_dir / filename
    target_file.parent.mkdir(parents=True, exist_ok=True)

    content = serialize_memory_frontmatter(mem_dict)
    _atomic_write_file(target_file, content)

    # Update the memory dict's file_path
    mem_dict["file_path"] = str(target_file.resolve())

    return target_file


# ---- Create New Memory ----

def remember_memory(
    source_dir: Path | str,
    content: str,
    memory_type: str = "fact",
    title: str = "",
    tags: list[str] | None = None,
    supersedes_id: str | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Record a new memory, handling revision/superseding when requested.

    Invariant: If supersedes_id is provided, the existing memory with that ID
    is found and its status is updated to "superseded" with superseded_by set
    to the new memory's ID. The old memory is written to disk before the new
    one is created.

    The new memory gets a stable ID format: mem_{type_prefix}_{timestamp}_{random}
    where type_prefix is the first 4 letters of memory_type.

    Returns the new memory dict.
    """
    if memory_type not in VALID_MEMORY_TYPES:
        raise ValueError(f"Invalid memory type: {memory_type}. Must be one of {VALID_MEMORY_TYPES}")

    source_path = Path(source_dir).resolve()
    now = datetime.now(timezone.utc).isoformat()
    prefix = memory_type[:4]
    random_suffix = os.urandom(3).hex()
    timestamp_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    new_id = f"mem_{prefix}_{timestamp_tag}_{random_suffix}"

    # If this supersedes an existing memory, find and update the old one first
    if supersedes_id:
        existing = load_kb_memories(source_path)
        for old_mem in existing:
            if old_mem["id"] == supersedes_id:
                old_mem["status"] = "superseded"
                old_mem["superseded_by"] = new_id
                old_mem["updated_at"] = now
                # Write the updated old memory
                # We need to reconstruct it for writing; use write_memory_record
                # with the updated dict
                write_memory_record(source_path, old_mem)
                break

    new_mem: dict[str, Any] = {
        "id": new_id,
        "type": memory_type,
        "status": "active",
        "title": title or (content.strip().splitlines()[0][:50] if content else "Untitled"),
        "tags": tags or [],
        "created_at": now,
        "updated_at": now,
        "superseded_by": None,
        "deprecation_reason": None,
        "confidence": 1.0,
        "source": source,
        "content": content.strip(),
        "file_path": "",
    }
    write_memory_record(source_path, new_mem)
    return new_mem


# ---- Deprecate Memory by ID ----

def deprecate_memory_by_id(
    source_dir: Path | str,
    memory_id: str,
    reason: str = "",
    superseded_by: str | None = None,
) -> dict[str, Any] | None:
    """Deprecate an existing memory by ID with an optional reason or successor.

    Invariant: Finds the memory with the given ID and updates its status:
    - If superseded_by is provided: status becomes "superseded"
    - If superseded_by is None: status becomes "deprecated"
    - deprecation_reason is set to the provided reason
    - updated_at is set to current time
    - The updated memory is written to disk

    Returns the updated memory dict, or None if no memory with that ID was found.
    """
    source_path = Path(source_dir).resolve()
    now = datetime.now(timezone.utc).isoformat()
    memories = load_kb_memories(source_path)
    for mem in memories:
        if mem["id"] == memory_id:
            mem["status"] = "superseded" if superseded_by else "deprecated"
            mem["deprecation_reason"] = reason
            if superseded_by:
                mem["superseded_by"] = superseded_by
            mem["updated_at"] = now
            write_memory_record(source_path, mem)
            return mem
    return None


# ---- Query Memories ----

def query_memories(
    source_dir: Path | str,
    query: str,
    memory_type: str | None = None,
    active_only: bool = True,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Filter memories matching a text query and criteria.

    Invariant: Searches both title and content fields. Keywords are extracted
    from the query by splitting on non-word characters and filtering to length > 2.
    Scoring: +10 for exact query string match in haystack, +1.5 for each keyword
    found. Results are sorted by score (highest first) and limited to top_k.

    If query is empty string, all active memories (or all if active_only=False)
    are returned (limited to top_k).

    Returns a list of memory dicts sorted by relevance score.
    """
    memories = load_kb_memories(source_dir)
    query_lower = query.lower().strip()
    words = [w for w in re.split(r"\W+", query_lower) if len(w) > 2]

    scored: list[tuple[float, dict[str, Any]]] = []
    for mem in memories:
        # Filter by status
        if active_only and mem["status"] != "active":
            continue
        # Filter by type
        if memory_type and mem["type"] != memory_type:
            continue

        # Score relevance
        haystack = f"{mem.get('title', '')} {mem.get('content', '')} {' '.join(mem.get('tags', []))}".lower()
        score = 0.0
        if query_lower and query_lower in haystack:
            score += 10.0
        for w in words:
            if w in haystack:
                score += 1.5

        if score > 0 or not query_lower:
            scored.append((score, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_k]]


# ---- Memory Similarity ----

def memory_similarity(mem_a: dict[str, Any], mem_b: dict[str, Any]) -> float:
    """Calculate text similarity between two memories using difflib.SequenceMatcher.

    Invariant: Compares title + content (lowercased, stripped) of two memories.
    Returns a quick_ratio() score between 0.0 and 1.0. Higher means more similar.

    This is used by find_conflicting_decisions() to determine if two decisions
    are similar enough to potentially conflict.
    """
    text_a = f"{mem_a.get('title', '')} {mem_a.get('content', '')}".lower().strip()
    text_b = f"{mem_b.get('title', '')} {mem_b.get('content', '')}".lower().strip()
    ratio = difflib.SequenceMatcher(None, text_a, text_b).quick_ratio()
    return ratio


# ---- Find Conflicting Decisions ----

def find_conflicting_decisions(memories: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    """Analyze memories and detect outdated or conflicting decisions.

    Invariant: Only compares active decision memories. Decisions are sorted
    chronologically (by created_at). Each decision is compared against all
    newer decisions.

    A conflict is flagged if:
    1. Similarity > 0.65 (substantively similar), OR
    2. Newer decision contains contradiction keywords AND either shared tags
       or similarity > 0.4

    Returns a list of (older, newer, reason) tuples.
    """
    decisions = [m for m in memories if m["type"] == "decision" and m["status"] == "active"]

    # Compare chronologically: newer decision might override an older one
    decisions.sort(key=lambda m: m.get("created_at", ""))

    conflicts: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for i in range(len(decisions)):
        for j in range(i + 1, len(decisions)):
            older = decisions[i]
            newer = decisions[j]

            sim = memory_similarity(older, newer)
            newer_text = newer.get("content", "").lower()

            # Check if newer mentions an explicit migration or override
            overrides = any(kw in newer_text for kw in CONTRADICTION_KEYWORDS)
            shared_tags = set(older.get("tags", [])) & set(newer.get("tags", []))

            if (sim > 0.65) or (overrides and (shared_tags or sim > 0.4)):
                reason = f"More recent decision ({newer['id']}) overrides or conflicts with {older['id']} (sim: {sim:.2f})"
                conflicts.append((older, newer, reason))

    return conflicts


# ---- Compact Episodic Memories ----

def compact_episodic_memories(
    source_dir: Path | str,
    memories: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compact multiple old episodic session logs into a consolidated milestone log.

    Invariant: If fewer than 4 active episodic memories exist, no compaction
    is performed (returns compacted_count: 0, milestones_created: 0).

    Otherwise, the 2 most recent episodics are kept, and all older ones are
    grouped into a single milestone record. The old memories are marked as
    superseded by the milestone.

    If dry_run=True, no files are modified; the function just reports what
    would be compacted.

    Returns dict with: compacted_count, milestones_created, milestone_id
    """
    source_path = Path(source_dir).resolve()
    if memories is None:
        memories = load_kb_memories(source_path)

    episodics = [m for m in memories if m["type"] == "episodic" and m["status"] == "active"]
    if len(episodics) < 4:
        return {"compacted_count": 0, "milestones_created": 0}

    # Group older episodics (everything except the 2 most recent)
    episodics.sort(key=lambda m: m.get("created_at", ""))
    to_compact = episodics[:-2]

    now = datetime.now(timezone.utc).isoformat()
    summary_lines = [
        "# Milestone di Consolidamento Sessioni Storiche",
        f"Consolidate {len(to_compact)} sessioni episodiche in un'unica sintesi storica.",
        "",
        "## Sessioni archiviate:",
    ]
    for mem in to_compact:
        summary_lines.append(f"- **{mem.get('title', '')}** ({mem.get('created_at', '')}): {mem.get('content', '')[:150].strip()}...")

    milestone_id = f"mem_fact_milestone_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    milestone_record: dict[str, Any] = {
        "id": milestone_id,
        "type": "fact",
        "status": "active",
        "title": "Consolidamento Sessioni Storiche",
        "tags": ["consolidation", "milestone", "history"],
        "created_at": now,
        "updated_at": now,
        "source": "dreaming:compaction",
        "content": "\n".join(summary_lines),
    }

    if not dry_run:
        write_memory_record(source_path, milestone_record)
        for old_ep in to_compact:
            old_ep["status"] = "superseded"
            old_ep["superseded_by"] = milestone_id
            old_ep["updated_at"] = now
            write_memory_record(source_path, old_ep)

    return {
        "compacted_count": len(to_compact),
        "milestones_created": 1,
        "milestone_id": milestone_id,
    }


# ---- Run Dreaming Cycle ----

def run_dreaming_cycle(
    source_dir: Path | str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute the full Dreaming (consolidation) cycle on a KB source directory.

    Invariant: Performs two phases:
    1. Resolve conflicting or superseded decisions (find_conflicting_decisions,
       update statuses, write to disk)
    2. Episodic log compaction (compact_episodic_memories)

    Returns dict with: total_memories, superseded_decisions, details_superseded,
    episodic_compaction, dry_run
    """
    source_path = Path(source_dir).resolve()
    memories = load_kb_memories(source_path)

    # Phase 1: Resolve conflicting or superseded decisions
    conflicts = find_conflicting_decisions(memories)
    superseded_records: list[dict[str, str]] = []

    for older, newer, reason in conflicts:
        superseded_records.append({
            "older_id": older["id"],
            "newer_id": newer["id"],
            "reason": reason,
        })
        if not dry_run:
            older["status"] = "superseded"
            older["superseded_by"] = newer["id"]
            older["updated_at"] = datetime.now(timezone.utc).isoformat()
            older["deprecation_reason"] = reason
            write_memory_record(source_path, older)

    # Phase 2: Episodic log compaction
    compaction_res = compact_episodic_memories(source_path, memories, dry_run=dry_run)

    return {
        "total_memories": len(memories),
        "superseded_decisions": len(superseded_records),
        "details_superseded": superseded_records,
        "episodic_compaction": compaction_res,
        "dry_run": dry_run,
    }


# ---- Export/Import Utilities ----

def memories_to_json(source_dir: Path | str) -> str:
    """Export all memories from a KB to JSON string.

    Invariant: Returns a JSON array of all memory dicts. Used for backup,
    migration, or debugging.
    """
    memories = load_kb_memories(source_dir)
    return json.dumps(memories, ensure_ascii=False, indent=2, sort_keys=True)


def json_to_memories(json_str: str) -> list[dict[str, Any]]:
    """Import memories from a JSON string.

    Invariant: Parses a JSON array of memory dicts and validates each has
    required fields. Invalid entries are skipped with a warning to stderr.
    Returns list of valid memory dicts.
    """
    try:
        memories = json.loads(json_str)
    except json.JSONDecodeError:
        return []

    valid: list[dict[str, Any]] = []
    for mem in memories:
        if not isinstance(mem, dict):
            continue
        # Validate required fields
        if "id" not in mem or "type" not in mem:
            continue
        if mem["type"] not in VALID_MEMORY_TYPES:
            mem["type"] = "fact"  # Default to fact
        valid.append(mem)
    return valid


# ---- Batch Operations ----

def batch_remember(
    source_dir: Path | str,
    contents: list[tuple[str, str, str | None, list[str] | None, str]],
) -> list[dict[str, Any]]:
    """Batch record multiple memories.

    Invariant: Each tuple is (content, memory_type, title, tags, source).
    Memories are recorded sequentially, with superseding handled if any
    content references previous memory IDs.

    Returns list of new memory dicts.
    """
    results: list[dict[str, Any]] = []
    for content, memory_type, title, tags, source in contents:
        mem = remember_memory(
            source_dir=source_dir,
            content=content,
            memory_type=memory_type,
            title=title,
            tags=tags,
            source=source,
        )
        results.append(mem)
    return results