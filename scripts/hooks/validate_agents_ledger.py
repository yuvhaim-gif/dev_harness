#!/usr/bin/env python3
"""Validate that AGENTS.md is loadable YAML with the required structure.

``check-yaml`` is filtered to ``\\.(yaml|yml)$`` and therefore never inspects
the YAML-bearing ``AGENTS.md``. This dedicated hook closes that gap (plan.md D1/F4).
"""

from __future__ import annotations

import sys
from typing import Any

import yaml

VALID_MODES = {"evolve", "isolated"}


def validate(path: str = "AGENTS.md") -> int:
    try:
        with open(path, encoding="utf-8") as f:
            ledger: Any = yaml.safe_load(f)
    except FileNotFoundError:
        print(f"ERROR: Missing operational ledger: {path}")
        return 1
    except yaml.YAMLError as exc:
        print(f"ERROR: {path} is not valid YAML: {exc}")
        return 1

    if not isinstance(ledger, dict):
        print(f"ERROR: {path} must be a YAML mapping at the top level.")
        return 1

    tasks = ledger.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        print(f"ERROR: {path} must define a non-empty 'tasks' mapping.")
        return 1

    ok = True
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            print(f"ERROR: task '{task_id}' must be a mapping.")
            ok = False
            continue
        mode = task.get("mutation_mode")
        if mode not in VALID_MODES:
            print(
                f"ERROR: task '{task_id}' has invalid mutation_mode "
                f"'{mode}' (expected one of {sorted(VALID_MODES)})."
            )
            ok = False

        contracts = set(task.get("contracts") or [])
        spec_docs = set(task.get("spec_docs") or [])
        if contracts - spec_docs:
            print(
                f"ERROR: task '{task_id}' lists contracts not in spec_docs: "
                f"{sorted(contracts - spec_docs)}."
            )
            ok = False

        contract_tests = set(task.get("contract_tests") or [])
        declared_tests = set(task.get("tests") or [])
        if contract_tests - declared_tests:
            print(
                f"ERROR: task '{task_id}' lists contract_tests not in tests: "
                f"{sorted(contract_tests - declared_tests)}."
            )
            ok = False

    if not ok:
        return 1

    print(f"OK: {path} parsed; {len(tasks)} task(s) validated.")
    return 0


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "AGENTS.md"
    return validate(path)


if __name__ == "__main__":
    sys.exit(main())
