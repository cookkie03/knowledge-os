"""Git-aware source file scanner.

Lists the files under a root directory, honoring nested .gitignore files when
the root is inside a git work tree, and falling back to a plain filesystem
walk otherwise. Never follows symlinks and never reports files whose real path
escapes the real root.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import subprocess


def list_source_files(root, exclude_patterns=None):
    """Return the files under root as a sorted dict with completeness info.

    The returned dict has keys:
      - "files": relative posix paths, sorted. Symlinks, missing tracked
        files, non-regular files, and anything outside the real root are
        excluded.
      - "complete": False when the scan could not finish (a git failure or an
        OSError during the walk). An incomplete scan must never be treated as
        authoritative by callers.
      - "errors": human-readable messages explaining why complete is False.
    """
    if exclude_patterns is None:
        exclude_patterns = []

    # Resolve the root once up front so every later path check is measured
    # against the same boundary; this blocks path traversal from escaping.
    root_real = os.path.realpath(root)

    files = []
    errors = []
    complete = True

    if _is_git_worktree(root_real):
        files, scan_errors = _git_list_files(root_real)
        errors.extend(scan_errors)
        if scan_errors:
            complete = False
    else:
        try:
            files = _walk_list_files(root_real)
        except OSError as exc:
            # A walk that hits an OSError may have partially listed the
            # tree; mark it incomplete so callers never trust a partial result.
            complete = False
            errors.append("filesystem walk failed: {}".format(exc))

    files = _apply_excludes(files, exclude_patterns)
    files.sort()

    return {"files": files, "complete": complete, "errors": errors}


def _is_git_worktree(root):
    """Return True when root is inside a git work tree.

    Uses git rev-parse so the answer reflects the real .git discovery rules,
    including worktrees and submodules, rather than a naive .git directory
    check that misses bare-style or nested layouts.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == b"true"


def _git_list_files(root):
    """List tracked and untracked files via git, honoring nested .gitignore.

    Uses git ls-files with -z so paths with special characters are delivered
    safely. Entries that are symlinks, missing (deleted-but-tracked), or not
    regular files are skipped silently: they must never be reported as
    scannable source files, and a missing tracked file is not a scan error.
    """
    files = []
    errors = []
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                root,
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
        )
    except OSError as exc:
        return files, ["git ls-files failed: {}".format(exc)]
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        if not message:
            message = "git ls-files failed"
        return files, ["git ls-files failed: {}".format(message)]

    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        rel = entry.decode("utf-8", "replace")
        abs_path = os.path.join(root, rel)
        if _skip_entry(abs_path, root):
            continue
        files.append(rel.replace(os.sep, "/"))
    return files, errors


def _skip_entry(abs_path, root_real):
    """Return True when abs_path must not be reported as a source file.

    Skips symlinks (never follow them), missing files (deleted-but-tracked
    entries must be skipped, not reported as errors), and anything that is not
    a regular file. Also rejects any file whose real path escapes the real
    root, which would be a path-traversal attempt.
    """
    if os.path.islink(abs_path):
        return True
    if not os.path.exists(abs_path):
        return True
    try:
        st = os.stat(abs_path)
    except OSError:
        return True
    if not stat.S_ISREG(st.st_mode):
        return True
    real = os.path.realpath(abs_path)
    if real != root_real and not real.startswith(root_real + os.sep):
        return True
    return False


def _walk_list_files(root):
    """Walk root without following symlinks, skipping .git directories.

    Used when root is not inside a git work tree. os.walk is called with
    followlinks=False so symlinked directories are never descended; symlinked
    files are filtered per entry. Any OSError raised while listing a
    directory propagates to the caller, which marks the scan incomplete.
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune .git directories in place so os.walk never descends into them;
        # modifying dirnames in place is the documented way to prune a walk.
        pruned = []
        for name in dirnames:
            if name != ".git":
                pruned.append(name)
        dirnames[:] = pruned
        for name in filenames:
            abs_path = os.path.join(dirpath, name)
            if _skip_entry(abs_path, root):
                continue
            rel = os.path.relpath(abs_path, root)
            files.append(rel.replace(os.sep, "/"))
    return files


def _apply_excludes(files, patterns):
    """Drop files matching any user-supplied exclude pattern.

    A pattern matches when fnmatch matches the whole relative path or any
    single path component, so "*.tmp" removes every .tmp file at any depth
    while "build/*" matches paths under build/.
    """
    if not patterns:
        return list(files)
    kept = []
    for rel in files:
        if _matches_any(rel, patterns):
            continue
        kept.append(rel)
    return kept


def _matches_any(rel, patterns):
    """Return True if rel matches any pattern by path or component."""
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        components = rel.split("/")
        for component in components:
            if fnmatch.fnmatch(component, pattern):
                return True
    return False