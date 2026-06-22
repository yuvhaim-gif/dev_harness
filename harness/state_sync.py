#!/usr/bin/env python3
"""Publish harness coordination state to a shared git ref.

Leases and handover journals committed only on an agent's work branch are
invisible to a fresh clone of ``main`` once that branch is abandoned. To make
coordination and handover survive across machines, this module mirrors those
files onto a dedicated ref (default ``harness-state``) using pure git plumbing:
a throwaway index builds a tree, ``commit-tree`` records it, and the commit is
pushed straight to the ref. Nothing touches the working tree or the current
branch, so it is safe to call from any state of the run.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
import time
from typing import Any

STATE_REF = os.getenv("AGENT_STATE_REF", "harness-state")

_REMOTE_PREFIX = "refs/remotes"


def log(msg: str) -> None:
    print(f"[state_sync] {msg}")


# Process-level memo of successful fetches, keyed by (repo_dir, remote, ref).
# Every shared-state read fetches the ref; without this a single run re-fetches
# the same ref many times. Writers pass ``refresh=True`` and reset the cache
# after a push so a build is never made on a stale read.
_fetch_cache: set[tuple[str, str, str]] = set()


def reset_fetch_cache() -> None:
    _fetch_cache.clear()


def _git(
    repo_dir: str,
    *args: str,
    env: dict[str, str] | None = None,
    text_input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        env=env,
        input=text_input,
    )


def _tracking_ref(remote: str, ref: str) -> str:
    return f"{_REMOTE_PREFIX}/{remote}/{ref}"


def fetch_ref(
    repo_dir: str, ref: str = STATE_REF, remote: str = "origin", refresh: bool = False
) -> bool:
    """Fetch ``ref`` from ``remote`` into a local tracking ref. False if absent."""
    key = (repo_dir, remote, ref)
    if not refresh and key in _fetch_cache:
        return True
    res = _git(
        repo_dir,
        "fetch",
        "--quiet",
        remote,
        f"+refs/heads/{ref}:{_tracking_ref(remote, ref)}",
    )
    if res.returncode != 0 and res.stderr.strip():
        # Distinguish a real auth/network failure from "no shared ref yet": a
        # missing ref fetches quietly, so a non-empty stderr is a genuine error.
        log(f"fetch of '{ref}' from '{remote}' failed: {res.stderr.strip()}")
    if res.returncode == 0:
        _fetch_cache.add(key)
    return res.returncode == 0


def read_file(repo_dir: str, path: str, ref: str = STATE_REF, remote: str = "origin") -> str | None:
    if not fetch_ref(repo_dir, ref, remote):
        return None
    res = _git(repo_dir, "show", f"{_tracking_ref(remote, ref)}:{path}")
    return res.stdout if res.returncode == 0 else None


def read_json(
    repo_dir: str, path: str, ref: str = STATE_REF, remote: str = "origin"
) -> dict[str, Any] | None:
    raw = read_file(repo_dir, path, ref, remote)
    if raw is None:
        return None
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def list_files(
    repo_dir: str, subdir: str, ref: str = STATE_REF, remote: str = "origin"
) -> list[str]:
    if not fetch_ref(repo_dir, ref, remote):
        return []
    res = _git(repo_dir, "ls-tree", "-r", "--name-only", _tracking_ref(remote, ref), subdir)
    if res.returncode != 0:
        return []
    return [line for line in res.stdout.splitlines() if line]


def publish_files(
    repo_dir: str,
    updates: dict[str, str | None],
    message: str,
    ref: str = STATE_REF,
    remote: str = "origin",
    attempts: int = 3,
    backoff_base: float = 0.5,
    backoff_cap: float = 8.0,
) -> bool:
    """Add/remove files on the shared ``ref`` and push.

    ``updates`` maps a repo-relative POSIX path to a local source file to add,
    or ``None`` to remove that path. Retries on a racing non-fast-forward push
    with bounded exponential backoff and jitter. Returns ``False`` when every
    attempt is exhausted; callers MUST treat that as a coordination failure
    (e.g. a lease release that did not propagate) rather than ignore it.
    """
    fd, index_path = tempfile.mkstemp(prefix="harness-index-")
    os.close(fd)
    os.remove(index_path)
    env = {**os.environ, "GIT_INDEX_FILE": index_path}

    total = max(1, attempts)
    try:
        for attempt in range(total):
            if attempt > 0 and backoff_base > 0:
                delay = min(backoff_cap, backoff_base * (2 ** (attempt - 1)))
                time.sleep(delay + random.uniform(0, backoff_base))
            fetched = fetch_ref(repo_dir, ref, remote, refresh=True)
            base = _tracking_ref(remote, ref) if fetched else None

            if base is not None:
                _git(repo_dir, "read-tree", base, env=env)
            else:
                _git(repo_dir, "read-tree", "--empty", env=env)

            for path, source in updates.items():
                if source is None:
                    _git(repo_dir, "update-index", "--force-remove", path, env=env)
                    continue
                blob = _git(repo_dir, "hash-object", "-w", "--", source, env=env)
                sha = blob.stdout.strip()
                if not sha:
                    continue
                _git(
                    repo_dir,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{sha},{path}",
                    env=env,
                )

            tree = _git(repo_dir, "write-tree", env=env).stdout.strip()
            if not tree:
                return False

            commit_args = ["commit-tree", tree]
            if base is not None:
                commit_args += ["-p", base]
            commit = _git(repo_dir, *commit_args, env=env, text_input=message).stdout.strip()
            if not commit:
                return False

            push = _git(repo_dir, "push", remote, f"{commit}:refs/heads/{ref}")
            if push.returncode == 0:
                # The shared ref advanced; drop memoized fetches so no later
                # read in this process is served from a now-stale tracking ref.
                reset_fetch_cache()
                return True
        return False
    finally:
        if os.path.exists(index_path):
            os.remove(index_path)
