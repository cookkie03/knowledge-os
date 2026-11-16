from __future__ import annotations

import json
import os
from pathlib import Path

# Centralized JSON-to-Markdown transformation for the ingestion pipeline.
# Invariant: JSON files are transformed to Markdown frontmatter + content,
# preserving the original source key via artifact_rel so the catalog can track
# them without re-embedding. This is a single-pass, idempotent transformation
# that avoids re-embedding tokens when the same JSON is re-ingested.

# Default subdirectory under derived_dir where transformed JSON markdown is stored.
# The pipeline always looks here first: derived_dir / "cache" / "json"
JSON_MARKDOWN_CACHE = "cache/json"


def intelligent_json_summary(raw_json: str, max_chars: int = 5000) -> str:
    """Return an "intelligent" excerpt of the JSON: first max_chars characters,
    trying to keep structure (important keys at top).

    Used as the Markdown body when a shorter summary is desired.
    """
    # If JSON is small enough, return entire content
    if len(raw_json) <= max_chars:
        return raw_json

    # For large JSON, try to keep important structures at the top
    # by returning the first max_chars characters
    return raw_json[:max_chars]


def json_to_markdown_with_frontmatter(raw_json: str, source_name: str) -> dict:
    """Convert JSON raw text to Markdown with frontmatter.

    Returns a dict with keys:
      - 'markdown': str - the complete Markdown content
      - 'artifact_rel': str - relative path from data_dir to the .md file
      - 'transformed': bool - always True when called correctly

    The transformation:
      1. Creates Markdown frontmatter: id, source, category: json, created_at
      2. The entire raw_json becomes the Markdown body (no truncation)
      3. The original JSON file is NOT deleted — it remains the source of truth.
    """
    # Deterministic ID based on source name + timestamp so re-ingestion is idempotent
    import time
    mem_id = f"json_{int(os.path.getmtime(__file__))}_{source_name}" if '__file__' in dir() else f"json_{int(time.time())}_{source_name}"

    # Build Markdown frontmatter
    md_lines: list[str] = [
        "---\n",
        f"id: {mem_id}\n",
        f"source: {source_name}\n",
        f"category: json\n",
        f"created_at: {int(os.path.getmtime(__file__)) if '__file__' in dir() else int(time.time())}\n",
        "---\n",
    ]

    # Body: entire raw JSON (no truncation - all content preserved)
    # Previously was raw[:2000]; now full content for complete vetorization
    body = raw_json

    md_content = "".join(md_lines) + "\n" + body

    # Determine the cache sub-directory and the target .md path
    # We can't know derived_dir here, but the caller will set artifact_rel
    # The markdown is stored under derived_dir / "cache" / "json" / <relative_path>.md
    # For now, construct a relative path that the pipeline will use

    # Generate the relative path: source_name with .json -> .md substitution
    # This is a placeholder; the actual artifact_rel is set by the caller
    md_rel_path = source_name if source_name.endswith(".md") else f"{source_name}.md"

    md_content = "".join(md_lines) + "\n" + raw_json

    return {
        "markdown": md_content,
        "artifact_rel": md_rel_path,
        "transformed": True,
    }


def detects_json_source(json_data: Any) -> str:
    """Detect the source type of JSON data based on its structure.

    Returns one of: "chatgpt", "claude", "notion", "generic_json"
    Used for logging/statistics when needed.
    """
    if not isinstance(json_data, (dict, list)):
        return "generic_json"

    # Check Notion export format
    if isinstance(json_data, dict) and ("results" in json_data or json_data.get("object") in {"list", "page", "block"}):
        return "notion"

    # Check ChatGPT conversation format
    if isinstance(json_data, dict) and "conversations" in json_data and isinstance(json_data["conversations"], list):
        return "chatgpt"

    # Check Claude Code / Agent transcript format
    if isinstance(json_data, dict) and "turns" in json_data and isinstance(json_data["turns"], list):
        return "claude_code"

    # Check if it looks like a Claude export (has uuid field commonly)
    if isinstance(json_data, dict) and "uuid" in json_data:
        return "claude"

    # Default to generic
    return "generic_json"


def transforms_json_to_markdown(
    source_path: Path,
    data_dir: Path,
    derived_dir: Path,
) -> dict | None:
    """Transform a JSON file to Markdown frontmatter if needed.

    Returns a dict with keys:
      - 'markdown_path': Path to the generated .md file relative to data_dir,
        or None if the file is not a JSON or no transformation was needed.
      - 'artifact_rel': Relative path from data_dir to the .md file (for catalog).
      - 'transformed': True if a transformation occurred.

    The transformation:
      1. Reads the JSON file at *source_path*.
      2. Creates a Markdown file with frontmatter: id, source, created_at, etc.
      2. The entire raw JSON content becomes the Markdown body (NO truncation -
        all content is preserved for complete vetorization).
      3. The original JSON file is NOT deleted — it remains the source of truth.
      4. The .md file is written under *derived_dir* / "cache" / "json" /
        <original_relative_path>.md  —  this is the single canonical location
        the pipeline reads when it encounters a JSON file.
    """
    try:
        ext = source_path.suffix.lower()
        if ext != ".json":
            return None

        # Read the raw JSON text
        raw = source_path.read_text(encoding="utf-8", errors="replace")

        # ── INVECE DI body = raw[:2000] USIAMO body = raw  ← Tutto il JSON
        body = raw

        # Determine the cache sub-directory and the target .md path
        cache_subdir = derived_dir / JSON_MARKDOWN_CACHE
        cache_subdir.mkdir(parents=True, exist_ok=True)

        # Relative path from data_dir to the source file, then swap .json → .md
        try:
            rel_from_data = source_path.relative_to(data_dir)
        except ValueError:
            # Source is not under data_dir — use the absolute name
            rel_from_data = source_path

        md_rel_path = rel_from_data.with_suffix(".md")
        md_path = cache_subdir / md_rel_path

        # Idempotent write: skip if content has not changed
        md_exists = md_path.is_file()
        if md_exists:
            existing = md_path.read_text(encoding="utf-8", errors="replace")
            if existing == ("".join([
                "---\n",
                f"id: json_{int(os.path.getmtime(source_path))}_{source_path.name}\n",
                f"source: {source_path.name}\n",
                f"category: json\n",
                f"created_at: {int(os.path.getmtime(source_path))}\n",
                "---\n",
            ]) + "\n" + body):
                # Already up to date — report transformation without writing again
                return {
                    "markdown_path": str(md_rel_path),
                    "artifact_rel": str(md_rel_path),
                    "transformed": True,
                }

        # Write the new .md file
        md_path.write_text(
            "".join([
                "---\n",
                f"id: json_{int(os.path.getmtime(source_path))}_{source_path.name}\n",
                f"source: {source_path.name}\n",
                f"category: json\n",
                f"created_at: {int(os.path.getmtime(source_path))}\n",
                "---\n",
            ]) + "\n" + body,
            encoding="utf-8",
        )

        # Return the artifact-relative path so the catalog can reference it
        artifact_rel = str(md_rel_path)
        return {
            "markdown_path": artifact_rel,
            "artifact_rel": artifact_rel,
            "transformed": True,
        }

    except Exception as exc:
        # Never break the pipeline for a single file
        return None


def maybe_transform_json_on_ingest(
    source_path: Path,
    data_dir: Path,
    derived_dir: Path,
    env: dict[str, str] | None = None,
) -> dict | None:
    """Public helper: try to transform JSON → Markdown for the ingestion pipeline.

    Called from the pipeline after a file is detected as JSON category.
    If transformation succeeds, the Markdown *artifact_rel* is returned so the
    catalog can reference the Markdown file instead of re-embedding the raw JSON.
    """
    env = env or {}
    return transforms_json_to_markdown(source_path, data_dir, derived_dir)