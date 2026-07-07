"""Shared lock policy used by both the pre-commit hook and the orchestrator.

Encoding the allowlist computation exactly once prevents hook/runner drift
(see plan.md B2). All ledger paths are expected to be POSIX, repo-root-relative.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

ALWAYS_LOCKED: frozenset[str] = frozenset({"AGENTS.md", ".pre-commit-config.yaml"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_truthy(value: str | None) -> bool:
    """True when ``value`` is one of the accepted truthy tokens (case-insensitive)."""
    return (value or "").strip().lower() in _TRUTHY


def env_flag(name: str) -> bool:
    """True when environment variable ``name`` holds a truthy token."""
    return is_truthy(os.getenv(name))


def human_override_active() -> bool:
    """True when a human has explicitly disabled the agent gates.

    Set ``SKIP_AGENT_HARNESS=1`` to let a developer make sweeping structural or
    configuration changes without the autonomous-agent allowlist blocking them.
    """
    return env_flag("SKIP_AGENT_HARNESS")


CONTRACT_LOCK_PATH = ".harness/contracts.lock"

COORDINATION_PREFIXES: tuple[str, ...] = (".harness/leases/", ".harness/journal/")

# Git records a symlink with this tree mode. An allowlisted *path* can be
# turned into a symlink (mode 100644 -> 120000) without ever leaving the
# allowlist, which lets an agent alias an allowed path onto a locked file and
# defeat the path-only lock gates. We therefore reject any agent-introduced
# symlink outright, regardless of where it points.
SYMLINK_MODE = "120000"


def symlink_paths(raw_diff: str) -> list[str]:
    """Paths whose resulting git mode is a symlink, from ``git diff --raw`` output.

    ``--raw`` lines look like::

        :<old_mode> <new_mode> <old_sha> <new_sha> <status>\\t<path>

    The result is a symlink iff ``new_mode`` is ``120000``. Mode is read from
    git's recorded tree entry (not ``os.path.islink``) so the check is portable
    and works against committed history on a CI runner where the link may not be
    materialised on disk.
    """
    out: list[str] = []
    for line in raw_diff.splitlines():
        if not line.startswith(":"):
            continue
        meta, _, path = line.partition("\t")
        fields = meta.split()  # [:old_mode, new_mode, old_sha, new_sha, status]
        if len(fields) >= 2 and fields[1] == SYMLINK_MODE and path:
            out.append(path)
    return out


class UnknownMutationModeError(ValueError):
    """Raised when a task declares an unsupported mutation_mode."""


def is_coordination_path(path: str) -> bool:
    """True for harness-managed coordination state (leases, journal).

    These are written and committed by the orchestrator itself, never by the
    LLM mutation, so they are always permitted regardless of the active task.
    """
    norm = path.replace("\\", "/")
    return any(norm.startswith(prefix) for prefix in COORDINATION_PREFIXES)


# Top-level keys the orchestrator itself writes for each coordination artifact.
# A payload under an exempt prefix is trusted ONLY when it is a flat JSON object
# whose keys are a subset of these -- anything else (a .py file, a renamed blob,
# or unknown-shaped JSON) is an attempt to smuggle content past the allowlist.
_COORDINATION_SCHEMA: dict[str, frozenset[str]] = {
    ".harness/leases/": frozenset(
        {"task_id", "branch", "agent_id", "base_commit", "targets", "created_at", "ttl_seconds"}
    ),
    ".harness/journal/": frozenset(
        {
            "task_id",
            "branch",
            "base_commit",
            "started_at",
            "attempts",
            "outcome",
            "notes",
            "finished_at",
        }
    ),
}


def _coordination_prefix(path: str) -> str | None:
    norm = path.replace("\\", "/")
    for prefix in COORDINATION_PREFIXES:
        if norm.startswith(prefix):
            return prefix
    return None


def is_valid_coordination_payload(path: str, blob: str | None) -> bool:
    """True iff ``path``/``blob`` is a well-formed harness coordination artifact.

    The allowlist exemption for ``.harness/leases/`` and ``.harness/journal/``
    is only safe when the committed content is what the orchestrator would have
    written: a flat ``*.json`` object (directly under the coordination dir, no
    nesting) whose top-level keys are a subset of the known schema. This blocks
    an attacker on a *directly pushed* branch -- one the local orchestrator and
    its SHA-based out-of-band check never saw -- from smuggling an arbitrary file
    (e.g. a ``.py`` payload) or unknown-shaped JSON under the exempt prefix.

    Content is *structurally* validated only; free-text fields (``notes``,
    attempt ``log_excerpt``) are still untrusted data and must be treated as such
    wherever they re-enter an LLM context (see prompt_builder's immutable rules).
    """
    prefix = _coordination_prefix(path)
    if prefix is None:
        return False
    rest = path.replace("\\", "/")[len(prefix) :]
    if "/" in rest or not rest.endswith(".json"):
        return False
    if blob is None:
        return False
    try:
        data: Any = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return set(data) <= _COORDINATION_SCHEMA[prefix]


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
