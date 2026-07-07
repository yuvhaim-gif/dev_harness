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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_context import hook_task_context, staged_files  # noqa: E402
from lock_policy import CONTRACT_LOCK_PATH  # noqa: E402


def main() -> None:
    context = hook_task_context("contract-binding gate")
    if context is None:
        sys.exit(0)
    task_id, task = context

    contracts = set(task.get("contracts") or [])
    contract_tests = set(task.get("contract_tests") or [])
    staged = set(staged_files())

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
