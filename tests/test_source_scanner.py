"""Tests for the git-aware source scanner."""

from __future__ import annotations

import os
import subprocess

import pytest

from pipeline.source_scanner import list_source_files


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_nested_gitignore_is_respected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "sub").mkdir()
    (repo / "sub" / ".gitignore").write_text("*.env\n")
    (repo / "sub" / "keep.txt").write_text("kept")
    (repo / "sub" / "secret.env").write_text("secret")
    (repo / "top.log").write_text("log")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    result = list_source_files(repo)
    assert result["complete"] is True
    assert result["errors"] == []
    files = result["files"]
    assert "sub/keep.txt" in files
    assert "sub/secret.env" not in files
    assert "top.log" not in files


def test_ignored_env_is_absent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".env").write_text("token")
    (repo / "keep.txt").write_text("kept")
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    result = list_source_files(repo)
    assert result["complete"] is True
    assert ".env" not in result["files"]
    assert "keep.txt" in result["files"]


def test_untracked_non_ignored_file_is_present(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.txt").write_text("a")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "untracked.txt").write_text("b")

    result = list_source_files(repo)
    assert result["complete"] is True
    assert result["files"] == ["tracked.txt", "untracked.txt"]


def test_symlinks_are_not_followed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "real.txt").write_text("a")
    os.symlink(repo / "real.txt", repo / "link.txt")
    (repo / "sub").mkdir()
    os.symlink(repo / "sub", repo / "sub_link")
    (repo / "sub" / "deep.txt").write_text("b")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    result = list_source_files(repo)
    assert result["complete"] is True
    assert result["files"] == ["real.txt", "sub/deep.txt"]


def test_non_git_directory_fallback_works(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("not-a-real-worktree")

    result = list_source_files(root)
    assert result["complete"] is True
    assert result["files"] == ["a.txt", "sub/b.txt"]


def test_user_exclude_patterns_work(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "keep.txt").write_text("a")
    (repo / "drop.tmp").write_text("b")
    (repo / "build").mkdir()
    (repo / "build" / "out.tmp").write_text("c")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    result = list_source_files(repo, exclude_patterns=["*.tmp", "build/*"])
    assert result["complete"] is True
    assert result["files"] == ["keep.txt"]


def test_git_failure_makes_scan_incomplete(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "keep.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    def fake_run(cmd, *args, **kwargs):
        # Only the ls-files call fails; the earlier worktree probe must still
        # succeed so the scanner takes the git path and hits the failure.
        if "ls-files" in cmd:
            class Result:
                returncode = 1
                stdout = b""
                stderr = b"boom"
            return Result()
        class ProbeResult:
            returncode = 0
            stdout = b"true\n"
            stderr = b""
        return ProbeResult()

    monkeypatch.setattr("pipeline.source_scanner.subprocess.run", fake_run)

    result = list_source_files(repo)
    assert result["complete"] is False
    assert result["errors"]
    assert result["files"] == []


def test_deleted_tracked_file_is_skipped_not_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "gone.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    (repo / "gone.txt").unlink()

    result = list_source_files(repo)
    assert result["complete"] is True
    assert result["errors"] == []
    assert result["files"] == []