#!/usr/bin/env python3
"""Abort commits that stage files outside the active task's allowlist."""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_context import hook_task_context, staged_files  # noqa: E402
from lock_policy import (  # noqa: E402
    UnknownMutationModeError,
    compute_allowlist,
    is_coordination_path,
    is_valid_coordination_payload,
    symlink_paths,
)


def _staged_blob(path: str) -> str | None:
    # The staged (index) content of ``path``; None when it is not in the index
    # (e.g. a staged deletion), which carries no payload to validate.
    res = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else None


def _staged_raw() -> str:
    # Mode-aware view of the index so a path turned into a symlink is visible.
    res = subprocess.run(
        ["git", "diff", "--cached", "--raw"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"ERROR: could not read git index: {res.stderr.strip()}")
        sys.exit(1)
    return res.stdout


def main() -> None:
    # Human override / no-agent-context / ledger + task resolution (shared gate).
    context = hook_task_context("file-lock gate")
    if context is None:
        sys.exit(0)
    task_id, task = context

    try:
        allowed = compute_allowlist(task)
    except UnknownMutationModeError as exc:
        print(f"ERROR: Unknown mutation_mode '{exc}' for task '{task_id}'.")
        sys.exit(1)

    mode = task.get("mutation_mode")

    # Symlinks bypass the path-only allowlist: an allowed path can be aliased
    # onto a locked file without ever leaving the allowlist. Reject outright.
    links = sorted(symlink_paths(_staged_raw()))
    if links:
        print(f"ERROR: task '{task_id}' ({mode}) staged symlink(s) (file-lock bypass):")
        for link in links:
            print(f"  - {link}")
        sys.exit(1)

    violations: list[str] = []
    bad_payloads: list[str] = []
    for staged in staged_files():
        if staged in allowed:
            continue
        if is_coordination_path(staged):
            blob = _staged_blob(staged)
            # A present coordination file is exempt only when it is a well-formed
            # artifact; arbitrary content under the exempt prefix is rejected.
            if blob is not None and not is_valid_coordination_payload(staged, blob):
                bad_payloads.append(staged)
            continue
        violations.append(staged)
    violations.sort()
    bad_payloads.sort()

    if bad_payloads:
        print(f"ERROR: task '{task_id}' ({mode}) staged invalid coordination payload(s):")
        for v in bad_payloads:
            print(f"  - {v}")
        print("Coordination paths must be the harness's own *.json lease/journal artifacts.")
    if violations:
        print(f"ERROR: task '{task_id}' ({mode}) staged files outside its allowlist:")
        for v in violations:
            print(f"  - {v}")
        print("Allowed:", ", ".join(sorted(allowed)) or "(none)")
    if violations or bad_payloads:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
