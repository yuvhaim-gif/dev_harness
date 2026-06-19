"""Shared lock policy used by both the pre-commit hook and the orchestrator.

Encoding the allowlist computation exactly once prevents hook/runner drift
(see plan.md B2). All ledger paths are expected to be POSIX, repo-root-relative.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ALWAYS_LOCKED: frozenset[str] = frozenset({"AGENTS.md", ".pre-commit-config.yaml"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def human_override_active() -> bool:
    """True when a human has explicitly disabled the agent gates.

    Set ``SKIP_AGENT_HARNESS=1`` to let a developer make sweeping structural or
    configuration changes without the autonomous-agent allowlist blocking them.
    """
    return (os.getenv("SKIP_AGENT_HARNESS") or "").strip().lower() in _TRUTHY


CONTRACT_LOCK_PATH = ".harness/contracts.lock"

COORDINATION_PREFIXES: tuple[str, ...] = (".harness/leases/", ".harness/journal/")


class UnknownMutationModeError(ValueError):
    """Raised when a task declares an unsupported mutation_mode."""


def is_coordination_path(path: str) -> bool:
    """True for harness-managed coordination state (leases, journal).

    These are written and committed by the orchestrator itself, never by the
    LLM mutation, so they are always permitted regardless of the active task.
    """
    norm = path.replace("\\", "/")
    return any(norm.startswith(prefix) for prefix in COORDINATION_PREFIXES)


def compute_allowlist(task: Mapping[str, Any]) -> set[str]:
    """Return the set of files the agent is permitted to stage for ``task``.

    - ``evolve``   -> targets | tests | spec_docs | the contract manifest
    - ``isolated`` -> targets only

    ``evolve`` may intentionally change a contract, so the hashed contract
    manifest (``.harness/contracts.lock``) is co-editable in that mode.
    ``isolated`` cannot touch the manifest, so any contract drift it causes is
    left to fail the contract tests.

    Explicitly locked files (``locked_files`` plus the always-locked set) are
    removed from the result, even if they appear elsewhere in the task.
    """
    mode = task.get("mutation_mode")
    targets = set(task.get("targets") or [])
    tests = set(task.get("tests") or [])
    spec_docs = set(task.get("spec_docs") or [])

    if mode == "evolve":
        allowed = targets | tests | spec_docs | {CONTRACT_LOCK_PATH}
    elif mode == "isolated":
        allowed = set(targets)
    else:
        raise UnknownMutationModeError(str(mode))

    explicit_locked = set(task.get("locked_files") or []) | set(ALWAYS_LOCKED)
    return allowed - explicit_locked
