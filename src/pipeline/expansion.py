"""Expand ZIP archives found among a source's physical files into logical
member files, so the ingestion planner can treat paths like
"docs/a.zip!/inner/b.md" like any other file.

Public functions:
  is_archive(path)          -> bool, True when the suffix is ".zip" (case-insensitive).
  expand_archives(root, rel_paths, work_dir, ...) -> dict with "plain", "members",
  "errors" and "complete".
"""

import os
from pathlib import Path

from pipeline.archive_parser import extract_archive


# Recognise archives by extension so the caller can separate them from ordinary
# files without opening any bytes.
def is_archive(path):
    return Path(path).suffix.lower() == ".zip"


# Expands every archive in rel_paths into a flat list of member files.
# Invariants:
#  - Never raises: every archive is wrapped in try/except and reported in errors.
#  - Extraction happens only inside work_dir, never next to the source files.
#  - Members are the last line of defence: each is re-checked to live inside its
#    own staging dir before it is accepted (Zip-Slip double guard).
def expand_archives(
    root,
    rel_paths,
    work_dir,
    max_depth=2,
    max_file_bytes=None,
    max_total_bytes=None,
    max_ratio=None,
):
    root_path = Path(root).resolve()
    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)

    plain = []
    members = []
    errors = []
    complete = True

    archive_index = 0
    for rel in rel_paths:
        rel_posix = rel.replace(os.sep, "/")
        if not is_archive(rel_posix):
            plain.append(rel_posix)
            continue

        archive_path = root_path / rel_posix
        staging_dir = work_path / ("a%03d" % archive_index)
        archive_index += 1

        # The archive itself must be a regular file inside the source root: a
        # crafted relative path or a symlink must never make us open a file
        # that lives elsewhere.
        if archive_path.is_symlink() or not archive_path.resolve().is_relative_to(root_path):
            errors.append(rel_posix + ": archive is not a regular file inside the source")
            complete = False
            continue

        try:
            kwargs = {"max_depth": max_depth}
            if max_file_bytes is not None:
                kwargs["max_file_bytes"] = max_file_bytes
            if max_total_bytes is not None:
                kwargs["max_total_bytes"] = max_total_bytes
            if max_ratio is not None:
                kwargs["max_ratio"] = max_ratio

            result = extract_archive(
                str(archive_path), str(staging_dir), **kwargs
            )
        except Exception as exc:
            errors.append(rel_posix + ": " + str(exc))
            complete = False
            continue

        for prefix in result["errors"]:
            errors.append(rel_posix + ": " + prefix)
        if not result["complete"]:
            complete = False

        archive_basename = Path(rel_posix).name
        provenance_prefix_len = len(archive_basename)
        staging_resolved = staging_dir.resolve()

        for entry in result["files"]:
            provenance = entry["provenance"]
            abs_path = Path(entry["path"]).resolve()

            # Last line of defence against Zip-Slip: even though extract_archive
            # already guards its own staging dir, a member must additionally live
            # inside *this* archive's staging dir before we hand it to ingestion.
            if not abs_path.is_relative_to(staging_resolved):
                errors.append("member escapes staging: " + provenance)
                complete = False
                continue

            # Nested archives were already expanded by extract_archive; their
            # members follow with a longer provenance chain, so skip the zip
            # itself rather than emitting it as a leaf file.
            if abs_path.suffix.lower() == ".zip":
                continue

            logical_path = rel_posix + provenance[provenance_prefix_len:]
            rel_inside = abs_path.relative_to(staging_resolved).as_posix()
            members.append(
                {
                    "path": logical_path,
                    "root": str(staging_resolved),
                    "rel": rel_inside,
                }
            )

    members.sort(key=lambda m: m["path"])
    return {
        "plain": plain,
        "members": members,
        "errors": errors,
        "complete": complete,
    }
