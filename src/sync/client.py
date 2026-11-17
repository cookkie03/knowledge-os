"""Build safe local source snapshots and compute transfer deltas."""

import hashlib
import os
from pathlib import Path

from pipeline.source_scanner import diff_snapshots


# Build a snapshot that marks uncertain scans incomplete to prevent false deletes.
#
# What this does: creates a snapshot of files in a folder without following symlinks,
# reporting any scan errors. The critical invariant is that any scan error (missing file,
# broken symlink, changed file, or a folder that moved during traversal) forces
# scan_complete to False. The caller treats an incomplete scan as "do not delete anything",
# so a partial or failing enumeration can never imply a deletion that the client did not
# explicitly authorize. This safety guard ensures clients never accidentally delete files
# they didn't intend to.
# Inputs/Outputs: folder (Path to scan) -> dict with "files" list, "scan_complete" bool,
# "scan_errors" list of error strings
def snapshot(folder):
    """Enumerate regular files without following symlinks and report scan errors.

    Invariant: any scan error (missing file, broken symlink, changed file, or a
    folder that moved during traversal) forces `scan_complete` to False. The
    caller treats an incomplete scan as "do not delete anything", so a partial
    or failing enumeration can never imply a deletion that the client did not
    explicitly authorize.
    """
    root = Path(folder)
    errors, files = [], []
    if not root.is_dir() or root.is_symlink():
        return {"files": [], "scan_complete": False, "scan_errors": ["source folder unavailable"]}

    try:
        root_identity = root.stat().st_dev, root.stat().st_ino
    except OSError as error:
        return {"files": [], "scan_complete": False, "scan_errors": [str(error)]}

    def onerror(error):
        """Make enumeration failures visible rather than implying deletions."""
        errors.append(str(error))

    # Keep traversal within real directories; any scan error disables deletion.
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        # Drop symlinked directories so os.walk does not follow them into the
        # same tree twice (or into a different tree entirely).
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                # Broken symlinks are reported as errors so an incomplete scan
                # blocks deletions instead of silently dropping the entry.
                if not path.exists():
                    errors.append(f"{path}: broken symlink")
                continue
            if not path.is_file():
                continue
            try:
                before = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = path.stat()
                # Re-stat after hashing to detect a file that changed while we
                # read it; a mutated file must not be treated as a stable snapshot.
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise OSError("file changed during scan")
                files.append({"path": path.relative_to(root).as_posix(), "size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()})
            except OSError as error:
                errors.append(f"{path}: {error}")
    try:
        final = root.stat()
        # If the root folder itself was replaced or became a symlink during the
        # walk, the snapshot is no longer trustworthy.
        if root.is_symlink() or (final.st_dev, final.st_ino) != root_identity:
            errors.append("source folder changed during scan")
    except OSError as error:
        errors.append(str(error))
    return {"files": sorted(files, key=lambda f: f["path"]), "scan_complete": not errors, "scan_errors": errors}


# The delta planner now lives in pipeline.source_scanner (single implementation);
# this alias keeps the legacy sync client working until ticket T9 removes it.
diff = diff_snapshots
