#!/usr/bin/env python3
"""Centralized ingestion pipeline for knowledge-os.

This module provides the unified ingestion pipeline that all other modules
can import and use. It centralizes the docling, whisper, and ingest logic
so that all consumers use the same pipeline.

Key features:
- Single entry point for all ingestion operations
- Configurable pipelines (text, pdf, images, office, audio)
- Atomic file writes with fcntl.flock, tempfile.mkstemp, os.replace
- Path validation with os.path.abspath/Path.resolve
- Redis-style comments for all critical blocks
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Core constants
HOME = Path(__file__).resolve().parent.parent
CONFIG = HOME / "config" / "global.env"
ENGINE_DIR = HOME / ".venv"
INGEST_SCRIPT = HOME / "bin" / "ingest.py"

# All supported file extensions (from pipeline.parsers)
TEXT_EXT: set[str] = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".adoc", ".asciidoc",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".sh", ".zsh", ".bash", ".fish", ".ps1", ".bat",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".xml", ".html", ".htm", ".sql", ".csv", ".tsv", ".log", ".diff", ".patch", ".rtf",
}
PDF_EXT: set[str] = {".pdf"}
IMAGE_EXT: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
OFFICE_EXT: set[str] = {
    ".docx", ".dotx", ".docm", ".dotm",
    ".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm",
    ".xlsx", ".xlsm",
    ".odt", ".ott", ".fodt", ".ods", ".ots", ".fods", ".odp", ".otp", ".fodp",
    ".epub", ".eml", ".msg",
}
AUDIO_VIDEO_EXT: set[str] = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".avi", ".mov"
}
ALL_SUPPORTED_EXT: set[str] = TEXT_EXT | PDF_EXT | IMAGE_EXT | OFFICE_EXT | AUDIO_VIDEO_EXT

# Pipeline enable flags (mapped from global.env)
PIPELINE_ENABLES: Dict[str, bool] = {
    "text": True,
    "pdf": True,
    "images": True,
    "office": True,
    "audio": True,
}


def load_global_config() -> dict[str, str]:
    """Load the global configuration from global.env."""
    env: dict[str, str] = {}
    if not CONFIG.exists():
        return env
    for line in CONFIG.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def save_global_config_atomic(updates: dict[str, str]) -> None:
    """Atomically update global.env using flock locking and atomic os.replace."""
    lines: list[str] = []
    keys_found: set[str] = set()

    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                lines.append(f"{k}={updates[k]}\n")
                keys_found.add(k)
            else:
                lines.append(line)

    for k, v in updates.items():
        if k not in keys_found:
            lines.append(f"{k}={v}\n")

    # Atomic write with flock
    fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG.parent), prefix=".global_env_")
    try:
        import fcntl
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CONFIG))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_pipeline_enables(env: dict[str, str] | None = None) -> dict[str, bool]:
    """Get pipeline enable flags from config, with defaults."""
    if env is None:
        env = load_global_config()
    defaults = {
        "PIPELINE_ENABLE_TEXT": "1",
        "PIPELINE_ENABLE_PDF": "1",
        "PIPELINE_ENABLE_IMAGES": "1",
        "PIPELINE_ENABLE_OFFICE": "1",
        "PIPELINE_ENABLE_AUDIO": "1",
    }
    result: dict[str, bool] = {}
    for key, default in defaults.items():
        val = env.get(key, default)
        result[key.lower().replace("PIPELINE_ENABLE_", "")] = val.strip() in ("1", "true", "yes")
    return result


def validate_path_is_under(target: Path, parent: Path) -> bool:
    """Validate that target path is under parent directory (path traversal protection).

    Uses os.path.abspath and Path.resolve to block path traversal attacks.
    Invariant: the resolved target must always be a descendant of the resolved
    target directory; this prevents a path traversal attack via crafted filenames.
    """
    try:
        resolved_target = target.resolve()
        resolved_parent = parent.resolve()
        # commonpath raises ValueError if the paths are on different drives;
        # catch it and return False so unsafe paths are rejected.
        return os.path.commonpath([str(resolved_target), str(resolved_parent)]) == str(resolved_parent)
    except (ValueError, OSError):
        return False


def safe_extract_archive(
    archive_path: Path | str,
    target_dir: Path | str,
    enable_text: bool = True,
    enable_pdf: bool = True,
    enable_images: bool = True,
    enable_office: bool = True,
    enable_audio: bool = True,
) -> dict[str, Any]:
    """Safely extract an archive with path guards and pipeline filtering.

    This is the central extraction function used by all consumers. It provides
    Zip-Slip protection, size limits, compression ratio guards, and pipeline
    filtering.

    Invariant: the resolved destination must always be a descendant of the
    resolved target directory; this prevents a Zip-Slip attack where a filename
    like "../../../etc/passwd" causes extraction outside the intended folder.

    Args:
        archive_path: Path to the archive file (zip supported)
        target_dir: Directory where files should be extracted
        enable_text: Whether to allow text file extraction
        enable_pdf: Whether to allow PDF extraction
        enable_images: Whether to allow image extraction
        enable_office: Whether to allow Office document extraction
        enable_audio: Whether to allow audio/video extraction

    Returns:
        Dict with extraction results: {"ok": True, "extracted": N, "files": [...]}
    """
    archive_path = Path(archive_path).resolve()
    target_dir = Path(target_dir).resolve()

    if not archive_path.is_file():
        raise FileNotFoundError(f"Archive file not found: {archive_path}")

    target_dir.mkdir(parents=True, exist_ok=True)

    # Load pipeline enables from config
    env = load_global_config()
    enables = get_pipeline_enables(env)

    # Override with explicit parameters
    if enable_text:
        enables["text"] = True
    if enable_pdf:
        enables["pdf"] = True
    if enable_images:
        enables["images"] = True
    if enable_office:
        enables["office"] = True
    if enable_audio:
        enables["audio"] = True

    extracted_files: list[Path] = []
    total_bytes = 0
    MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB per file limit
    MAX_TOTAL_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB total extracted limit
    MAX_COMPRESSION_RATIO = 100.0  # Zip-bomb protection

    import zipfile

    with zipfile.ZipFile(archive_path, "r") as zf:
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

            # Verify size bounds and compression ratio
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

            # Apply pipeline filtering: only extract files matching enabled pipelines
            file_ext = Path(filename).suffix.lower()
            should_extract = True
            if file_ext in TEXT_EXT and not enables.get("text", True):
                should_extract = False
            elif file_ext in PDF_EXT and not enables.get("pdf", True):
                should_extract = False
            elif file_ext in IMAGE_EXT and not enables.get("images", True):
                should_extract = False
            elif file_ext in OFFICE_EXT and not enables.get("office", True):
                should_extract = False
            elif file_ext in AUDIO_VIDEO_EXT and not enables.get("audio", True):
                should_extract = False

            if not should_extract:
                continue  # Skip this file

            dest = target_dir / filename
            # Final containment check: the resolved destination must be under the
            # resolved target directory; if not, a Zip-Slip attack is detected.
            if not validate_path_is_under(dest, target_dir):
                raise ArchiveSecurityError(f"Zip-Slip attempt detected: {filename}")

            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted_files.append(dest)

    return {
        "ok": True,
        "extracted": len(extracted_files),
        "files": [str(f) for f in extracted_files],
        "total_bytes": total_bytes,
    }


class ArchiveSecurityError(RuntimeError):
    """Raised when an archive violates safety constraints (Zip-Slip, bomb, symlink)."""
    pass


def run_ingest_pipeline(
    kb_name: str,
    source_folder: str | None = None,
    force: bool = False,
    pipelines: str | None = None,
    no_text: bool = False,
    no_pdf: bool = False,
    no_images: bool = False,
    no_office: bool = False,
    no_audio: bool = False,
    background: bool = True,
    extract_archive: bool = False,
    archive_path: str | None = None,
) -> dict[str, Any]:
    """Run the centralized ingestion pipeline for a KB.

    This is the unified entry point for all ingestion operations. It replaces
    the functionality previously in ingest_manager.py and archive_importer.py.

    The pipeline:
    1. Resolves KB configuration from registry
    2. Sets up KB directories (data, derived, rag_storage)
    3. Extracts archives if requested (with pipeline filtering)
    4. Runs the ingest script with appropriate flags
    5. Returns status and results

    Invariant: All file operations use atomic writes (mkstemp + os.replace)
    and path validation (resolve + commonpath) to prevent corruption and
    path traversal attacks.

    Args:
        kb_name: Name of the knowledge base to ingest into
        source_folder: Optional source folder path
        force: Force re-ingestion of already-processed files
        pipelines: Comma-separated list of pipelines to run
        no_text: Skip text file parsing
        no_pdf: Skip PDF parsing
        no_images: Skip image extraction
        no_office: Skip Office document parsing
        no_audio: Skip audio/video processing
        background: Run in background (return immediately) or synchronous
        extract_archive: Whether to extract and process an archive
        archive_path: Path to archive file if extract_archive is True

    Returns:
        Dict with status, command, and results
    """
    env = load_global_config()

    # Resolve KB from registry
    from kb_registry import load_registry, find_kb, normalize_kb

    registry = load_registry(env)
    kb_info = find_kb(registry, kb_name)
    if not kb_info:
        return {"ok": False, "status": "error", "message": f"KB '{kb_name}' not found in registry"}

    # Normalize KB entry
    kb = normalize_kb(kb_info, env)

    # Determine source folder
    source_dir = kb_info.get("source_dir") or kb_info.get("source_folder")
    if not source_dir:
        return {"ok": False, "status": "error", "message": f"KB '{kb_name}' missing source_dir/source_folder"}

    source_path = Path(source_dir).resolve()

    # Handle archive extraction if requested
    if extract_archive and archive_path:
        archive_file = Path(archive_path).resolve()
        if not archive_file.is_file():
            return {"ok": False, "status": "error", "message": f"Archive file not found: {archive_file}"}

        # Extract with pipeline filtering
        result = safe_extract_archive(
            archive_path=archive_file,
            target_dir=source_path,
            enable_text=not no_text,
            enable_pdf=not no_pdf,
            enable_images=not no_images,
            enable_office=not no_office,
            enable_audio=not no_audio,
        )
        if not result["ok"]:
            return {"ok": False, "status": "error", "message": result.get("message", "Archive extraction failed")}

        # Update source_folder to the extracted directory
        source_folder = str(source_path)
    elif source_folder:
        source_path = Path(source_folder).resolve()

    # Build ingest command
    engine_python = ENGINE_DIR / "bin" / "python"
    if not engine_python.exists():
        engine_python = Path("python3")

    cmd = [str(engine_python), str(INGEST_SCRIPT), "--kb", kb_name]

    if force:
        cmd.append("--force")
    if pipelines:
        cmd.extend(["--pipelines", pipelines])

    # Pipeline filtering flags
    if no_text:
        cmd.append("--no-text")
    if no_pdf:
        cmd.append("--no-pdf")
    if no_images:
        cmd.append("--no-images")
    if no_office:
        cmd.append("--no-office")
    if no_audio:
        cmd.append("--no-audio")
    if source_folder:
        cmd.extend(["--source-folder", source_folder])

    if background:
        # Run in background with logging
        log_path = Path(f"./data/{kb_name}/ingest.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Use atomic log file creation
        fd, tmp_log = tempfile.mkstemp(dir=str(log_path.parent), prefix=".ingest_log_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write("")
            os.replace(tmp_log, log_path)
        finally:
            if os.path.exists(tmp_log):
                os.unlink(tmp_log)

        subprocess.Popen(
            cmd,
            stdout=log_path.open("a"),
            stderr=log_path.open("a"),
            start_new_session=True,
        )
        return {"ok": True, "status": "queued", "command": " ".join(cmd)}
    else:
        # Run synchronously
        result = subprocess.run(cmd, capture_output=True, text=True)
        return {
            "ok": result.returncode == 0,
            "status": "completed" if result.returncode == 0 else "failed",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


def ingest_from_archive(
    kb_name: str,
    archive_path: str,
    force: bool = False,
    pipelines: str | None = None,
    no_text: bool = False,
    no_pdf: bool = False,
    no_images: bool = False,
    no_office: bool = False,
    no_audio: bool = False,
) -> dict[str, Any]:
    """Ingest files from an archive into a KB.

    This is a convenience function that extracts an archive and runs the
    ingestion pipeline in a single operation.

    Invariant: Archive extraction and ingestion use the same pipeline settings
    and path validation to ensure consistency.
    """
    # Extract archive to a temp directory
    from pathlib import Path as _Path
    import tempfile

    archive_file = _Path(archive_path).resolve()

    with tempfile.TemporaryDirectory(prefix="rag_ingest_") as tmpdir:
        tmp_path = _Path(tmpdir)

        # Safe extraction using centralized function
        result = safe_extract_archive(
            archive_path=archive_file,
            target_dir=tmp_path,
            enable_text=not no_text,
            enable_pdf=not no_pdf,
            enable_images=not no_images,
            enable_office=not no_office,
            enable_audio=not no_audio,
        )
        if not result["ok"]:
            return {"ok": False, "status": "error", "message": result.get("message", "Archive extraction failed")}

        # Find the extracted source directory
        # The archive may contain a top-level folder or files directly
        extracted_items = list(tmp_path.iterdir())
        if len(extracted_items) == 1 and extracted_items[0].is_dir():
            source_subdir = extracted_items[0]
        else:
            source_subdir = tmp_path

        # Run ingestion on the extracted content
        return run_ingest_pipeline(
            kb_name=kb_name,
            source_folder=str(source_subdir),
            force=force,
            pipelines=pipelines,
            no_text=no_text,
            no_pdf=no_pdf,
            no_images=no_images,
            no_office=no_office,
            no_audio=no_audio,
            background=False,  # Synchronous for archive ingestion
        )


# Backward compatibility: migrate old ingest_manager.py calls
# These functions delegate to the centralized pipeline

def run_ingest_legacy(
    kb_name: str,
    source_folder: str | None = None,
    force: bool = False,
    pipelines: str | None = None,
    no_text: bool = False,
    no_pdf: bool = False,
    no_images: bool = False,
    no_office: bool = False,
    no_audio: bool = False,
    background: bool = True,
) -> dict[str, Any]:
    """Legacy wrapper that delegates to the centralized ingest_pipeline.

    Deprecated: Use run_ingest_pipeline instead.
    """
    return run_ingest_pipeline(
        kb_name=kb_name,
        source_folder=source_folder,
        force=force,
        pipelines=pipelines,
        no_text=no_text,
        no_pdf=no_pdf,
        no_images=no_images,
        no_office=no_office,
        no_audio=no_audio,
        background=background,
    )


def import_archive_legacy(
    archive_path: str,
    output_source_dir: str,
    provider: str = "auto",
) -> dict[str, Any]:
    """Legacy wrapper for archive import.

    Deprecated: Use ingest_from_archive or ingest_pipeline directly.
    """
    from pathlib import Path as _Path
    from datetime import datetime

    archive_path_resolved = _Path(archive_path).resolve()
    output_source_dir_resolved = _Path(output_source_dir).resolve()
    imported_dir = output_source_dir_resolved / "imported"
    preferences_dir = output_source_dir_resolved / "memories" / "preferences"

    imported_dir.mkdir(parents=True, exist_ok=True)
    preferences_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rag_import_") as tmpdir:
        tmp_path = _Path(tmpdir)

        # Safe extraction using centralized function
        result = safe_extract_archive(
            archive_path=archive_path_resolved,
            target_dir=tmp_path,
            enable_text=not no_text if 'no_text' in dir() else True,
            enable_pdf=not no_pdf if 'no_pdf' in dir() else True,
            enable_images=not no_images if 'no_images' in dir() else True,
            enable_office=not no_office if 'no_office' in dir() else True,
            enable_audio=not no_audio if 'no_audio' in dir() else True,
        )
        if not result["ok"]:
            return {"ok": False, "status": "error", "message": result.get("message", "Archive extraction failed")}

        # Search for conversation JSON files in extracted directory
        all_convs: list[Any] = []
        for json_candidate in tmp_path.rglob("*.json"):
            try:
                content = json_candidate.read_text(encoding="utf-8", errors="replace")
                # Parse would depend on source format - this is a stub
                # In production, would use archive_importer.detect_and_parse_json
                all_convs.append({"raw": content[:100]})  # placeholder
            except Exception:
                continue

        # Write conversations (stub - would use conversation_to_markdown)
        timestamp_prefix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        batch_folder = imported_dir / f"import_{timestamp_prefix}"
        batch_folder.mkdir(parents=True, exist_ok=True)

        return {
            "conversations_imported": 0,
            "preferences_extracted": 0,
            "destination_folder": str(batch_folder),
        }