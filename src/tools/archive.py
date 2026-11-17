"""Safe archive importer and normalizer for ChatGPT, Claude, and Gemini exports.

Extracts archives with Zip-Slip protection, bounded size/ratio enforcement,
and normalizes message trees into clean Markdown documents with frontmatter.
Also extracts stable user preferences and instructions. The JSON structure
detection and Markdown rendering live in pipeline.json_parser (single
implementation shared with the ingestion dispatcher).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.json_parser import conversation_to_markdown, detect_and_parse_json

MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB per file limit
MAX_TOTAL_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB total extracted limit
MAX_COMPRESSION_RATIO = 100.0  # Zip-bomb protection: uncompressed/compressed ratio


class ArchiveSecurityError(RuntimeError):
    """Raised when an archive violates safety constraints (Zip-Slip, bomb, symlink)."""
    pass


# Check destination containment to block directory traversal / Zip-Slip attacks.
#
# What this does: validates that the resolved destination path is always a descendant of the
# resolved target directory, preventing Zip-Slip attacks where filenames like
# "../../../etc/passwd" cause extraction outside the intended folder. Uses os.path.commonpath
# to verify containment, catching ValueError/OSError and returning False to reject unsafe entries.
# This is a critical security guard at the extraction trust boundary.
# Inputs/Outputs: target_dir (Path to extract into), dest_path (Path of extracted file) -> bool
# True if safe (dest is descendant of target), False if unsafe or on error
def _is_safe_extraction_path(target_dir: Path, dest_path: Path) -> bool:
    try:
        resolved_target = target_dir.resolve()
        resolved_dest = dest_path.resolve()
        # commonpath raises ValueError if the paths are on different drives;
        # catch it and return False so unsafe entries are rejected.
        return os.path.commonpath([str(resolved_target), str(resolved_dest)]) == str(resolved_target)
    except (ValueError, OSError):
        return False


# Unpack a zip archive strictly inside target_dir with decompression and path guards.
#
# What this does: extracts a zip archive into target_dir with multiple safety guards
# against Zip-Slip, zip bombs, symlinks, size limits, total byte limits, and unsafe
# compression ratios. Each guard raises ArchiveSecurityError on violation so the
# caller knows exactly what failed. The invariant is that all extracted files must
# remain within target_dir; any path traversal attempt triggers an immediate security
# error.
# Inputs/Outputs: zip_path (Path to zip archive), target_dir (Path to extract into) ->
# list[Path] of successfully extracted files, raises ArchiveSecurityError on violation
def safe_extract_zip(zip_path: Path | str, target_dir: Path | str) -> list[Path]:
    zip_path = Path(zip_path).resolve()
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    if not zip_path.is_file():
        raise FileNotFoundError(f"Archive file not found: {zip_path}")

    extracted_files: list[Path] = []
    total_bytes = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            # Block symlinks and directory entries masquerading as files.
            # Upper 16 bits of external_attr contain UNIX file mode: 0o120000 = S_IFLNK.
            is_symlink = (info.external_attr >> 16) & 0o120000 == 0o120000
            if is_symlink:
                raise ArchiveSecurityError(f"Symlinks are forbidden in archive: {info.filename}")

            filename = info.filename
            # Block relative traversal components ('..') at any path segment;
            # prevents a Zip-Slip attack via crafted archive entries.
            if ".." in filename.split("/") or ".." in filename.split("\\"):
                raise ArchiveSecurityError(f"Relative traversal ('..') detected in entry: {filename}")

            # Reject entries that would be written with an absolute path; that
            # would overwrite arbitrary filesystem locations.
            if filename.startswith("/") or filename.startswith("\\"):
                raise ArchiveSecurityError(f"Absolute path entry detected in archive: {filename}")

            # Verify size bounds and compression ratio: MAX_FILE_SIZE_BYTES limits
            # individual files, MAX_TOTAL_EXTRACTED_BYTES limits the whole archive,
            # and MAX_COMPRESSION_RATIO defends against zip bombs where a small
            # compressed file expands to many gigabytes.
            if info.file_size > MAX_FILE_SIZE_BYTES:
                raise ArchiveSecurityError(
                    f"File {filename} exceeds max size limit ({info.file_size} > {MAX_FILE_SIZE_BYTES})"
                )

            total_bytes += info.file_size
            if total_bytes > MAX_TOTAL_EXTRACTED_BYTES:
                raise ArchiveSecurityError(
                    f"Total extracted size exceeds limit ({total_bytes} > {MAX_TOTAL_EXTRACTED_BYTES})"
                )

            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO and info.file_size > 1024 * 1024:
                    raise ArchiveSecurityError(f"Compression ratio too high ({ratio:.1f}) for entry: {filename}")

            dest = target_dir / filename
            # Final containment check: the resolved destination must be under the
            # resolved target directory; if not, a Zip-Slip attack is detected.
            if not _is_safe_extraction_path(target_dir, dest):
                raise ArchiveSecurityError(f"Zip-Slip attempt detected: {filename}")

            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted_files.append(dest)

    return extracted_files


# Sanitize title to create a safe filesystem filename.
def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text.strip().lower())
    slug = re.sub(r"[\s_-]+", "-", cleaned)[:60].strip("-")
    return slug or "conversation"


# Heuristic patterns identifying stable user instructions, preferences, and environment facts.
USER_INSTRUCTION_PATTERNS = [
    re.compile(r"(?:ricordati|ricorda|tieni a mente|non dimenticare)\s+che\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:preferisco|prediligo|la mia preferenza è)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:nelle mie macchine|sul mio server|nel mio ambiente|uso solitamente)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:il mio ruolo è|lavoro come|sono uno?)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:always remember|please remember|my preference is|i always use)\s+(.+)", re.IGNORECASE),
]


# Extract preference candidates from user turns across conversations.
def extract_user_preferences(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for conv in conversations:
        for msg in conv["messages"]:
            if msg["role"] not in {"user", "human"}:
                continue
            text = msg["content"]
            matched_for_message = False
            for pat in USER_INSTRUCTION_PATTERNS:
                if matched_for_message:
                    break
                for match in pat.finditer(text):
                    snippet = match.group(0).strip()
                    # Keep snippet bounded and avoid duplicates
                    if len(snippet) < 15 or len(snippet) > 600:
                        continue
                    key = snippet.lower()
                    if key in seen_hashes:
                        continue
                    seen_hashes.add(key)
                    extracted.append({
                        "content": snippet,
                        "source_conv_id": conv["id"],
                        "source_conv_title": conv["title"],
                        "timestamp": msg["timestamp"] or conv["created_at"],
                    })
                    matched_for_message = True
                    break

    return extracted


# Process an archive file or directory and write normalized markdown + preferences into target_dir.
def import_archive_to_kb(
    archive_path: Path | str,
    output_source_dir: Path | str,
    provider: str = "auto",
) -> dict[str, Any]:
    archive_path = Path(archive_path).resolve()
    output_source_dir = Path(output_source_dir).resolve()
    imported_dir = output_source_dir / "imported"
    preferences_dir = output_source_dir / "memories" / "preferences"

    imported_dir.mkdir(parents=True, exist_ok=True)
    preferences_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rag_import_") as tmpdir:
        tmp_path = Path(tmpdir)
        if archive_path.is_file() and archive_path.suffix.lower() == ".zip":
            safe_extract_zip(archive_path, tmp_path)
        elif archive_path.is_dir():
            # Directory input: copy safe files
            for root, _, files in os.walk(archive_path):
                for f in files:
                    src_f = Path(root) / f
                    rel = src_f.relative_to(archive_path)
                    dest_f = tmp_path / rel
                    dest_f.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_f, dest_f)
        else:
            raise ValueError(f"Unsupported archive format: {archive_path}")

        # Search for conversation JSON files in extracted directory
        all_convs: list[dict[str, Any]] = []
        for json_candidate in tmp_path.rglob("*.json"):
            try:
                content = json_candidate.read_text(encoding="utf-8", errors="replace")
                convs = detect_and_parse_json(content)
                if convs:
                    all_convs.extend(convs)
            except Exception:
                continue

        # Export conversations into markdown documents
        written_convs = 0
        timestamp_prefix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        batch_folder = imported_dir / f"import_{timestamp_prefix}"
        batch_folder.mkdir(parents=True, exist_ok=True)

        for conv in all_convs:
            # Imported chats are stored as episodic memories, hence the extra keys.
            md_content = conversation_to_markdown(conv, ["type: episodic", "status: active"])
            filename = f"{slugify(conv['title'])}_{conv['id'][:8]}.md"
            (batch_folder / filename).write_text(md_content, encoding="utf-8")
            written_convs += 1

        # Extract user preferences and save to memories/preferences/
        preferences = extract_user_preferences(all_convs)
        written_prefs = 0
        for idx, pref in enumerate(preferences):
            pref_id = f"pref_{timestamp_prefix}_{idx:03d}"
            pref_content = pref["content"]
            md = [
                "---",
                f"id: {pref_id}",
                "type: preference",
                "status: active",
                f"title: {json.dumps(pref_content[:40])}",
                f"created_at: {json.dumps(datetime.now(timezone.utc).isoformat())}",
                f"source: {json.dumps('import:' + pref['source_conv_title'])}",
                "---",
                f"# Preferenza Utente Estratta",
                "",
                pref_content,
                "",
            ]
            pref_file = preferences_dir / f"{pref_id}.md"
            pref_file.write_text("\n".join(md), encoding="utf-8")
            written_prefs += 1

        return {
            "conversations_imported": written_convs,
            "preferences_extracted": written_prefs,
            "destination_folder": str(batch_folder),
        }
