import os
import zipfile
from pathlib import Path

# Limits are deliberately conservative defaults; callers may tighten them.
MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
# Zip-bomb protection: uncompressed bytes divided by compressed bytes.
MAX_COMPRESSION_RATIO = 100.0
# Streaming chunk size: small enough that a lying header is caught quickly.
CHUNK_SIZE = 64 * 1024


# Blocks Zip-Slip: the resolved destination must always be a descendant of the
# resolved target directory, otherwise a name like "../../etc/passwd" would
# write outside the staging area.
def _is_safe_extraction_path(target_dir, dest_path):
    try:
        resolved_target = Path(target_dir).resolve()
        resolved_dest = Path(dest_path).resolve()
        common = os.path.commonpath([str(resolved_target), str(resolved_dest)])
        return common == str(resolved_target)
    except (ValueError, OSError):
        # Unresolvable paths are rejected, never trusted.
        return False


# Records a failure and marks the whole extraction as incomplete, so callers
# never treat a partial extraction as authoritative.
def _fail(state, message):
    state["errors"].append(message)
    state["complete"] = False


# Copies one entry to disk while counting the bytes ACTUALLY produced.
# Invariant: the limits are enforced on real decompressed bytes, not on the
# sizes declared in the zip header, because a malicious header can lie.
# Side effect: on any violation the partial file is deleted.
# Returns the number of bytes written, or -1 when the entry was rejected.
def _write_entry_streaming(zf, info, dest, state):
    written = 0
    rejected = ""
    out = None
    try:
        out = open(dest, "wb")
        src = zf.open(info, "r")
        try:
            while True:
                chunk = src.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > state["max_file_bytes"]:
                    rejected = "File too large: " + info.filename
                    break
                if state["total_written"] + written > state["max_total_bytes"]:
                    rejected = "Total size limit exceeded by " + info.filename
                    break
                if info.compress_size > 0:
                    ratio = written / info.compress_size
                    if ratio > state["max_ratio"]:
                        rejected = "Compression ratio too high: " + info.filename
                        break
                elif written > state["max_ratio"] * 1024:
                    # A zero compressed size cannot give a ratio, so cap it hard.
                    rejected = "Suspicious zero-size payload: " + info.filename
                    break
                out.write(chunk)
        finally:
            src.close()
    finally:
        if out is not None:
            out.close()

    if rejected != "":
        try:
            Path(dest).unlink(missing_ok=True)
        except OSError:
            pass
        _fail(state, rejected)
        return -1
    return written


# Validates one zip entry name and returns the safe destination, or None.
# Invariant: symlinks, "..", absolute paths and escapes are all rejected.
def _check_entry(info, dest_root, provenance, state):
    is_symlink = ((info.external_attr >> 16) & 0o120000) == 0o120000
    if is_symlink:
        _fail(state, "Symlink detected: " + info.filename + " in " + provenance)
        return None

    filename = info.filename
    parts_slash = filename.split("/")
    parts_back = filename.split("\\")
    if ".." in parts_slash or ".." in parts_back:
        _fail(state, "Traversal attempt: " + filename + " in " + provenance)
        return None

    if filename.startswith("/") or filename.startswith("\\"):
        _fail(state, "Absolute path entry: " + filename + " in " + provenance)
        return None

    dest = (Path(dest_root) / filename).resolve()
    if not _is_safe_extraction_path(dest_root, dest):
        _fail(state, "Zip-Slip attempt: " + filename + " in " + provenance)
        return None
    return dest


# Extracts one archive and recurses into nested .zip files.
# Invariant: every extracted file lives inside dest_root, and recursion depth
# is bounded by state["max_depth"].
def _extract_one(target_zip, dest_root, depth, provenance, state):
    if depth > state["max_depth"]:
        _fail(state, "Max depth " + str(state["max_depth"]) + " reached at " + provenance)
        return

    if not target_zip.exists():
        _fail(state, "Archive not found: " + str(target_zip))
        return

    try:
        zf = zipfile.ZipFile(target_zip, "r")
    except Exception as e:
        _fail(state, "Failed to process " + provenance + ": " + str(e))
        return

    try:
        for info in zf.infolist():
            dest = _check_entry(info, dest_root, provenance, state)
            if dest is None:
                continue

            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            written = _write_entry_streaming(zf, info, dest, state)
            if written < 0:
                continue

            # Only real bytes count toward the running total.
            state["total_written"] += written
            current_prov = provenance + "!/" + info.filename
            state["files"].append({"path": str(dest), "provenance": current_prov})

            if dest.suffix.lower() == ".zip":
                _extract_one(dest, dest.parent, depth + 1, current_prov, state)
    except Exception as e:
        _fail(state, "Failed to process " + provenance + ": " + str(e))
    finally:
        zf.close()


# Public entry point. Never raises: problems are reported in "errors" and set
# "complete" to False. Extraction happens only inside staging_dir, never next
# to the source archive.
# Returns {"files": [{"path", "provenance"}], "errors": [...], "complete": bool}.
def extract_archive(
    archive_path,
    staging_dir,
    max_depth=2,
    max_file_bytes=MAX_FILE_SIZE_BYTES,
    max_total_bytes=MAX_TOTAL_EXTRACTED_BYTES,
    max_ratio=MAX_COMPRESSION_RATIO,
):
    state = {
        "files": [],
        "errors": [],
        "complete": True,
        "total_written": 0,
        "max_depth": max_depth,
        "max_file_bytes": max_file_bytes,
        "max_total_bytes": max_total_bytes,
        "max_ratio": max_ratio,
    }

    staging = Path(staging_dir).resolve()
    staging.mkdir(parents=True, exist_ok=True)

    archive = Path(archive_path).resolve()
    _extract_one(archive, staging, 0, archive.name, state)

    return {
        "files": state["files"],
        "errors": state["errors"],
        "complete": state["complete"],
    }
