#!/usr/bin/env python3
"""Shared preamble for the ``AGENT_TASK_ID``-gated pre-commit hooks.

The file-lock and contract-binding hooks open with the same three-step gate:
honour the human override, skip when there is no agent context, then load the
ledger and resolve the active task. Encoding that once keeps their behaviour and
error wording in lock-step. Also holds the git-index reader both hooks share.

Depends only on the standard library plus the sibling ``ledger`` / ``lock_policy``
modules, so it is safe to import from a pre-commit hook running in its isolated
``pyyaml``-only virtualenv.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

from ledger import LedgerError, get_task, load_ledger
from lock_policy import human_override_active


def staged_files() -> list[str]:
    """Repo-root-relative paths staged in the index (git emits POSIX separators)."""
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"ERROR: could not read git index: {res.stderr.strip()}")
        sys.exit(1)
    return [line for line in res.stdout.splitlines() if line]


def hook_task_context(gate_label: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve the active task for a gated hook, or ``None`` when it must not gate.

    Returns ``(task_id, task)`` for a normal agent commit. Returns ``None`` when
    the caller should exit 0 without gating -- either a human override is active
    (``gate_label`` names this hook in the printed notice) or there is no agent
    context. Exits 1 with a clean message on a malformed/absent ledger or an
    unknown task, matching the previous per-hook behaviour.
    """
    if human_override_active():
        print(f"SKIP_AGENT_HARNESS set: human override -- {gate_label} bypassed.")
        return None

    task_id = os.getenv("AGENT_TASK_ID")
    if not task_id:
        return None

    try:
        ledger = load_ledger()
    except LedgerError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    task = get_task(ledger, task_id)
    if not task:
        print(f"ERROR: Task '{task_id}' not found in AGENTS.md.")
        sys.exit(1)
    return task_id, task
