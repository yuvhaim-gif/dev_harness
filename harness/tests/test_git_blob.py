"""Unit tests for the shared ``git_blob.read_blob`` helper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "harness"))

from git_blob import read_blob  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def _seed(repo: Path) -> str:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_read_blob_returns_content_at_ref(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert read_blob(str(tmp_path), "HEAD", "a.txt") == "hello\n"


def test_read_blob_missing_path_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert read_blob(str(tmp_path), "HEAD", "does_not_exist.txt") is None


def test_read_blob_bad_ref_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert read_blob(str(tmp_path), "no-such-ref", "a.txt") is None


def test_read_blob_tracks_ref_history(tmp_path: Path) -> None:
    first = _seed(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "update")
    assert read_blob(str(tmp_path), first, "a.txt") == "hello\n"
    assert read_blob(str(tmp_path), "HEAD", "a.txt") == "changed\n"
