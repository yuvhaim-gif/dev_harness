#!/usr/bin/env python3
"""Optimistic staleness guard: never push onto a moved contract.

Before reconciling, the orchestrator compares the *critical* files (contracts,
spec docs, the always-locked policy files, the contract manifest, and any
declared ``locked_files``) as they were at the agent's base commit against the
shared ref (``origin/main``). If any critical file moved on the shared ref
since the agent branched, the work is stale: rather than push a change built on
a superseded contract, the run stops and hands the conflict to the next agent.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Any

from lock_policy import ALWAYS_LOCKED, CONTRACT_LOCK_PATH


def critical_paths(task: Mapping[str, Any]) -> set[str]:
    paths: set[str] = set(ALWAYS_LOCKED) | {CONTRACT_LOCK_PATH}
    paths |= set(task.get("contracts") or [])
    paths |= set(task.get("spec_docs") or [])
    paths |= set(task.get("locked_files") or [])
    # The task's own targets are critical too: if two agents race the lease for an
    # isolated-mode task, the loser of the lease race is still caught here when a
    # target it built on has since moved on the shared ref.
    paths |= set(task.get("targets") or [])
    return paths


def _blob_at(repo_dir: str, ref: str, path: str) -> str | None:
    res = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout


def changed_between(repo_dir: str, base_ref: str, head_ref: str, paths: set[str]) -> list[str]:
    """Critical ``paths`` whose content differs between two refs."""
    changed: list[str] = []
    for path in sorted(paths):
        before = _blob_at(repo_dir, base_ref, path)
        after = _blob_at(repo_dir, head_ref, path)
        if before != after:
            changed.append(path)
    return changed


def check(
    repo_dir: str,
    base_commit: str,
    shared_ref: str,
    task: Mapping[str, Any],
) -> list[str]:
    """Return the critical files that moved on ``shared_ref`` since the agent
    branched at ``base_commit`` (empty == safe to push)."""
    return changed_between(repo_dir, base_commit, shared_ref, critical_paths(task))
