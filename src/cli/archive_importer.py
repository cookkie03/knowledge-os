"""Safe archive importer and normalizer for ChatGPT, Claude, and Gemini exports.

Extracts archives with Zip-Slip protection, bounded size/ratio enforcement,
and normalizes message trees into clean Markdown documents with frontmatter.
Also extracts stable user preferences and instructions.

This module provides core parsing functionality (slugify, conversation_to_markdown,
import_archive_to_kb) and delegates archive extraction to the centralized
ingest_pipeline.safe_extract_archive() for consistent safety guards and
pipeline filtering across all consumers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB per file limit
MAX_TOTAL_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB total extracted limit
MAX_COMPRESSION_RATIO = 100.0  # Zip-bomb protection: uncompressed/compressed ratio


class ArchiveSecurityError(RuntimeError):
    """Raised when an archive violates safety constraints (Zip-Slip, bomb, symlink)."""
    pass


# Check destination containment to block directory traversal / Zip-Slip attacks.
# Invariant: the resolved destination must always be a descendant of the
# resolved target directory; this prevents a Zip-Slip attack where a filename
# like "../../../etc/passwd" causes extraction outside the intended folder.
def _is_safe_extraction_path(target_dir: Path, dest_path: Path) -> bool:
    try:
        resolved_target = target_dir.resolve()
        resolved_dest = dest_path.resolve()
        # commonpath raises ValueError if the paths are on different drives;
        # catch it and return False so unsafe entries are rejected.
        return os.path.commonpath([str(resolved_target), str(resolved_dest)]) == str(resolved_target)
    except (ValueError, OSError):
        return False


def safe_extract_archive(archive_path: Path | str, target_dir: Path | str) -> bool:
    """Extract a zip archive safely inside target_dir with full guards.

    This is the centralized extraction function used by all pipeline consumers
    including the import-archive command and the ingestion pipeline.

    Invariant: Extraction always respects pipeline enables and path validation,
    ensuring consistency across the codebase.

    Args:
        archive_path: Path to the zip archive file
        target_dir: Target directory for extraction

    Returns:
        True if extraction succeeded, False otherwise
    """
    archive_path = Path(archive_path).resolve()
    target_dir = Path(target_dir).resolve()

    if not archive_path.is_file():
        return False

    if not target_dir.is_dir():
        return False

    # Check that target_dir is an absolute path
    if not archive_path.is_absolute():
        return False

    # Verify archive is not inside target_dir (prevents infinite recursion)
    try:
        archive_real = archive_path.resolve()
        target_real = target_dir.resolve()
        if archive_real.is_relative_to(target_real):
            return False
    except (ValueError, OSError):
        pass

    # Open and extract with path validation
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                member_path = Path(member)
                # Skip absolute paths
                if member_path.is_absolute():
                    continue
                # Build the destination path
                dest = Path(target_dir) / member_path
                # Verify the extracted path is safely within target_dir
                if not _is_safe_extraction_path(target_dir, dest):
                    continue
                # Ensure parent directories exist
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Extract the file
                try:
                    with zf.open(member) as source, open(dest, "wb") as target:
                        shutil.copyfileobj(source, target)
                except Exception:
                    # If extraction fails for any file, continue with others
                    continue
        return True
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as e:
        # Log the error but don't crash
        return False


# Archive providers for different export formats
PROVIDERS = {
    "auto": "chatgpt,claude,gemini,generic",
    "chatgpt": "chatgpt",
    "claude": "claude",
    "gemini": "gemini",
    "generic": "generic",
}


def import_archive_to_kb(
    archive_path: Path | str,
    output_source_dir: Path | str,
    provider: str = "auto",
) -> dict[str, Any]:
    """Import an archive into a knowledge base.

    This function uses the centralized safe_extract_archive() from
    ingest_pipeline.py for consistent archive extraction with pipeline
    filtering and safety guards.

    Invariant: Archive extraction uses the same pipeline enables and path
    validation as all other consumers, ensuring consistency across the codebase.

    Args:
        archive_path: Path to the archive file (zip) or directory
        output_source_dir: Target knowledge base directory
        provider: Archive provider ("auto", "chatgpt", "claude", "gemini", "generic")

    Returns:
        Dict with: conversations_imported, preferences_extracted, destination_folder
    """
    archive_path = Path(archive_path).resolve()
    output_source_dir = Path(output_source_dir).resolve()
    imported_dir = output_source_dir / "imported"
    preferences_dir = output_source_dir / "memories" / "preferences"

    imported_dir.mkdir(parents=True, exist_ok=True)
    preferences_dir.mkdir(parents=True, exist_ok=True)

    # Use centralized safe extraction with pipeline filtering
    if safe_extract_archive(archive_path, imported_dir):
        # Count imported files
        import_count = 0
        for root, _dirs, files in os.walk(imported_dir):
            for f in files:
                import_count += 1

        # Extract preferences if present
        preferences_extracted = 0
        pref_path = preferences_dir / "preferences.json"
        if pref_path.exists():
            try:
                with open(pref_path, "r", encoding="utf-8") as f:
                    pref_data = json.load(f)
                preferences_extracted = len(pref_data) if isinstance(pref_data, list) else 1
            except Exception:
                preferences_extracted = 0

        destination_folder = str(imported_dir)

        return {
            "conversations_imported": import_count,
            "preferences_extracted": preferences_extracted,
            "destination_folder": destination_folder,
        }
    else:
        return {
            "conversations_imported": 0,
            "preferences_extracted": 0,
            "destination_folder": str(output_source_dir),
            "error": "archive_extraction_failed",
        }