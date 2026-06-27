#!/usr/bin/env python3
"""Server-side re-enforcement of the file-lock + contract guarantees.

The pre-commit hooks run on the agent's machine and can, in principle, be
skipped by an agent that does its own git (``-c core.hooksPath=...`` or
plumbing). This script re-applies the *same* policy against the aggregate diff
of a pushed branch, from a trusted CI runner the agent cannot influence:

  1. the hashed contract manifest must still verify (no silent drift),
  2. every file changed on an ``agent/<task_id>/...`` branch must fall inside
     that task's computed allowlist (coordination paths excepted), and
  3. a contract changed on an agent branch must carry a change to at least one
     of that task's bound ``contract_tests`` -- the same binding the local
     ``enforce_contract_binding`` hook applies, re-checked here so it holds even
     when the local hook was skipped (this runner ignores ``SKIP_AGENT_HARNESS``).

A human (non-agent) branch only gets the manifest check; its file scope and
bound-test discipline are the reviewer's responsibility, not the harness's.

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
    symlink_paths,
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


def _changed_symlinks(base: str, head: str) -> list[str]:
    # Mode-aware diff so an allowlisted path flipped to a symlink is caught.
    res = _git("diff", "--raw", f"{base}...{head}")
    if res.returncode != 0:
        res = _git("diff", "--raw", f"{base}..{head}")
    if res.returncode != 0:
        return []
    return symlink_paths(res.stdout)


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

    links = sorted(_changed_symlinks(args.base, args.head))
    if links:
        failed = True
        print(f"FAIL: task '{task_id}' introduced symlink(s) (file-lock bypass):")
        for path in links:
            print(f"  - {path}")

    changed = _changed_files(args.base, args.head)
    violations = sorted(f for f in changed if f not in allowed and not is_coordination_path(f))
    if violations:
        failed = True
        print(f"FAIL: task '{task_id}' changed files outside its allowlist:")
        for path in violations:
            print(f"  - {path}")
        print("Allowed:", ", ".join(sorted(allowed)) or "(none)")
    elif not links:
        print(f"OK: all {len(changed)} changed file(s) are within '{task_id}' scope.")

    # 3. Contract<->test binding, re-applied server-side. enforce_contract_binding
    #    runs locally but is skippable by an agent that does its own git; this
    #    re-check holds on the trusted runner so a contract change that omits its
    #    bound test cannot pass green on a directly pushed or orphaned branch.
    #    (The manifest half of the binding is covered by step 1 above.)
    raw_contracts = task.get("contracts") or []
    raw_tests = task.get("contract_tests") or []
    contracts = set(raw_contracts) if isinstance(raw_contracts, list) else set()
    contract_tests = set(raw_tests) if isinstance(raw_tests, list) else set()
    changed_set = set(changed)
    touched_contracts = sorted(changed_set & contracts)
    if touched_contracts and contract_tests and not (changed_set & contract_tests):
        failed = True
        print(
            f"FAIL: task '{task_id}' changed a contract "
            f"({', '.join(touched_contracts)}) without updating any bound "
            "contract_test: " + ", ".join(sorted(contract_tests))
        )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
