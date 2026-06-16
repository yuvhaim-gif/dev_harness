"""Shared lock policy used by both the pre-commit hook and the orchestrator.

Encoding the allowlist computation exactly once prevents hook/runner drift
(see plan.md B2). All ledger paths are expected to be POSIX, repo-root-relative.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ALWAYS_LOCKED: frozenset[str] = frozenset({"AGENTS.md", ".pre-commit-config.yaml"})


class UnknownMutationModeError(ValueError):
    """Raised when a task declares an unsupported mutation_mode."""


def compute_allowlist(task: Mapping[str, Any]) -> set[str]:
    """Return the set of files the agent is permitted to stage for ``task``.

    - ``evolve``   -> targets | tests | spec_docs
    - ``isolated`` -> targets only

    Explicitly locked files (``locked_files`` plus the always-locked set) are
    removed from the result, even if they appear elsewhere in the task.
    """
    mode = task.get("mutation_mode")
    targets = set(task.get("targets") or [])
    tests = set(task.get("tests") or [])
    spec_docs = set(task.get("spec_docs") or [])

    if mode == "evolve":
        allowed = targets | tests | spec_docs
    elif mode == "isolated":
        allowed = set(targets)
    else:
        raise UnknownMutationModeError(str(mode))

    explicit_locked = set(task.get("locked_files") or []) | set(ALWAYS_LOCKED)
    return allowed - explicit_locked
