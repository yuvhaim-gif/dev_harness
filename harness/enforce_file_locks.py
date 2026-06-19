#!/usr/bin/env python3
"""Abort commits that stage files outside the active task's allowlist."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_policy import (  # noqa: E402
    UnknownMutationModeError,
    compute_allowlist,
    human_override_active,
    is_coordination_path,
)


def _staged_files() -> list[str]:
    # git emits POSIX-style, repo-root-relative paths on every OS.
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"ERROR: could not read git index: {res.stderr.strip()}")
        sys.exit(1)
    return [line for line in res.stdout.splitlines() if line]


def main() -> None:
    # Explicit human override: a developer disabling the gates for sweeping work.
    if human_override_active():
        print("SKIP_AGENT_HARNESS set: human override -- file-lock gate bypassed.")
        sys.exit(0)

    # Humans committing normally (no agent context) are not gated.
    task_id = os.getenv("AGENT_TASK_ID")
    if not task_id:
        sys.exit(0)

    try:
        with open("AGENTS.md", encoding="utf-8") as f:
            ledger = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print("ERROR: Missing operational ledger: AGENTS.md")
        sys.exit(1)
    except yaml.YAMLError as exc:
        # A malformed ledger must abort cleanly, not dump a traceback.
        print(f"ERROR: AGENTS.md is not valid YAML: {exc}")
        sys.exit(1)

    task: Any = (ledger.get("tasks") or {}).get(task_id)
    if not task:
        print(f"ERROR: Task '{task_id}' not found in AGENTS.md.")
        sys.exit(1)

    try:
        allowed = compute_allowlist(task)
    except UnknownMutationModeError as exc:
        print(f"ERROR: Unknown mutation_mode '{exc}' for task '{task_id}'.")
        sys.exit(1)

    mode = task.get("mutation_mode")
    violations = sorted(
        f for f in _staged_files() if f not in allowed and not is_coordination_path(f)
    )
    if violations:
        print(f"ERROR: task '{task_id}' ({mode}) staged files outside its allowlist:")
        for v in violations:
            print(f"  - {v}")
        print("Allowed:", ", ".join(sorted(allowed)) or "(none)")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
