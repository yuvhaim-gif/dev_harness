#!/usr/bin/env python3
"""Require contract changes to carry their manifest + bound-test updates.

Layer B of the contract<->test binding. When an agent stages a change to any of
its task's ``contracts``, this hook insists the same commit also:

  1. updates the hashed manifest (``.harness/contracts.lock``), so the change is
     a recorded, intentional contract revision; and
  2. touches at least one of the task's declared ``contract_tests`` (when the
     task declares any), so the rules that pin the contract move with it.

Humans (no ``AGENT_TASK_ID``) are never gated.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lock_policy import CONTRACT_LOCK_PATH, human_override_active  # noqa: E402


def _staged_files() -> set[str]:
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"ERROR: could not read git index: {res.stderr.strip()}")
        sys.exit(1)
    return {line for line in res.stdout.splitlines() if line}


def main() -> None:
    if human_override_active():
        print("SKIP_AGENT_HARNESS set: human override -- contract-binding gate bypassed.")
        sys.exit(0)

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
        print(f"ERROR: AGENTS.md is not valid YAML: {exc}")
        sys.exit(1)

    task: Any = (ledger.get("tasks") or {}).get(task_id)
    if not task:
        print(f"ERROR: Task '{task_id}' not found in AGENTS.md.")
        sys.exit(1)

    contracts = set(task.get("contracts") or [])
    contract_tests = set(task.get("contract_tests") or [])
    staged = _staged_files()

    touched_contracts = sorted(staged & contracts)
    if not touched_contracts:
        sys.exit(0)

    problems: list[str] = []
    if CONTRACT_LOCK_PATH not in staged:
        problems.append(
            f"contract changed but {CONTRACT_LOCK_PATH} was not updated "
            "(run: python harness/contract_manifest.py --update)"
        )
    if contract_tests and not (staged & contract_tests):
        problems.append(
            "contract changed but none of its bound contract_tests were updated: "
            + ", ".join(sorted(contract_tests))
        )

    if problems:
        print(f"ERROR: task '{task_id}' changed a contract ({', '.join(touched_contracts)}):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
