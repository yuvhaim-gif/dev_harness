#!/usr/bin/env python3
"""Server-side re-enforcement of the file-lock + contract guarantees.

The pre-commit hooks run on the agent's machine and can, in principle, be
skipped by an agent that does its own git (``-c core.hooksPath=...`` or
plumbing). This script re-applies the *same* policy against the aggregate diff
of a pushed branch, from a trusted CI runner the agent cannot influence:

  1. the hashed contract manifest must still verify (no silent drift), and
  2. every file changed on an ``agent/<task_id>/...`` branch must fall inside
     that task's computed allowlist (coordination paths excepted).

A human (non-agent) branch only gets the manifest check; its file scope is the
reviewer's responsibility, not the harness's.

Usage:
    python harness/ci_enforce.py [--base <ref>] [--head <ref>] [--task <id>]

Refs and the task default from the GitHub Actions environment
(``GITHUB_BASE_REF`` / ``GITHUB_HEAD_REF``) and from the head branch name.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import contract_manifest  # noqa: E402
from lock_policy import (  # noqa: E402
    UnknownMutationModeError,
    compute_allowlist,
    is_coordination_path,
)

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - yaml is a declared dependency
    print("ERROR: PyYAML is required to run ci_enforce.")
    sys.exit(1)

_AGENT_BRANCH = re.compile(r"^agent/(?P<task_id>.+)/[^/]+$")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _current_branch() -> str:
    res = _git("rev-parse", "--abbrev-ref", "HEAD")
    return res.stdout.strip() if res.returncode == 0 else ""


def _task_from_branch(branch: str) -> str | None:
    match = _AGENT_BRANCH.match(branch)
    return match.group("task_id") if match else None


def _changed_files(base: str, head: str) -> list[str]:
    # `base...head` = changes on head since it diverged from base (PR semantics).
    res = _git("diff", "--name-only", f"{base}...{head}")
    if res.returncode != 0:
        # Fall back to a two-dot range if the merge base cannot be found.
        res = _git("diff", "--name-only", f"{base}..{head}")
    if res.returncode != 0:
        print(f"ERROR: could not diff {base}...{head}: {res.stderr.strip()}")
        sys.exit(1)
    return [line for line in res.stdout.splitlines() if line]


def _load_task(task_id: str) -> dict[str, object] | None:
    try:
        with open("AGENTS.md", encoding="utf-8") as fh:
            ledger = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        print("ERROR: Missing operational ledger: AGENTS.md")
        sys.exit(1)
    except yaml.YAMLError as exc:
        print(f"ERROR: AGENTS.md is not valid YAML: {exc}")
        sys.exit(1)
    task = (ledger.get("tasks") or {}).get(task_id)
    return task if isinstance(task, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI-side file-lock + contract re-check.")
    base_default = os.getenv("GITHUB_BASE_REF") or "origin/main"
    head_default = os.getenv("GITHUB_HEAD_REF") or "HEAD"
    parser.add_argument("--base", default=base_default)
    parser.add_argument("--head", default=head_default)
    parser.add_argument("--task", default=os.getenv("AGENT_TASK_ID"))
    args = parser.parse_args(argv)

    failed = False

    # 1. Contract manifest must still verify (content-based; bypass-proof).
    manifest_problems = contract_manifest.verify()
    if manifest_problems:
        failed = True
        print("FAIL: contract manifest is out of date:")
        for problem in manifest_problems:
            print(f"  - {problem}")
    else:
        print("OK: contract manifest verifies.")

    # 2. Re-apply the allowlist to the aggregate diff of agent branches.
    head_branch = args.head if args.head != "HEAD" else _current_branch()
    task_id = args.task or _task_from_branch(head_branch)

    if task_id is None:
        print(f"SKIP: '{head_branch}' is not an agent branch; file-scope check skipped.")
        return 1 if failed else 0

    task = _load_task(task_id)
    if task is None:
        print(f"FAIL: task '{task_id}' (from branch) not found in AGENTS.md.")
        return 1

    try:
        allowed = compute_allowlist(task)
    except UnknownMutationModeError as exc:
        print(f"FAIL: task '{task_id}' has unknown mutation_mode '{exc}'.")
        return 1

    changed = _changed_files(args.base, args.head)
    violations = sorted(f for f in changed if f not in allowed and not is_coordination_path(f))
    if violations:
        failed = True
        print(f"FAIL: task '{task_id}' changed files outside its allowlist:")
        for path in violations:
            print(f"  - {path}")
        print("Allowed:", ", ".join(sorted(allowed)) or "(none)")
    else:
        print(f"OK: all {len(changed)} changed file(s) are within '{task_id}' scope.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
