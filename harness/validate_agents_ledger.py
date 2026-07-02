#!/usr/bin/env python3
"""Validate that AGENTS.md is loadable YAML with the required structure.

``check-yaml`` is filtered to ``\\.(yaml|yml)$`` and therefore never inspects
the YAML-bearing ``AGENTS.md``. This dedicated hook closes that gap (plan.md D1/F4).
"""

from __future__ import annotations

import os
import sys
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: E402

import okf  # noqa: E402

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
    if not isinstance(tasks, dict):
        print(f"ERROR: {path} must define a 'tasks' mapping.")
        return 1
    if not tasks:
        # Empty skeleton produced by 'python -m harness --init'; valid but
        # not yet runnable. Operators fill in tasks before invoking the loop.
        print(f"OK: {path} parsed; empty 'tasks' skeleton (no tasks defined yet).")
        return 0

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

        attempts = task.get("max_autorepair_attempts")
        if attempts is not None and (isinstance(attempts, bool) or not isinstance(attempts, int)):
            print(f"ERROR: task '{task_id}' max_autorepair_attempts must be an integer.")
            ok = False
        for field_name in (
            "spec_docs",
            "tests",
            "targets",
            "locked_files",
            "contracts",
            "contract_tests",
            "pr_labels",
        ):
            value = task.get(field_name)
            if value is not None and not isinstance(value, list):
                print(f"ERROR: task '{task_id}' field '{field_name}' must be a list.")
                ok = False

        contracts = set(task.get("contracts") or [])
        spec_docs = set(task.get("spec_docs") or [])
        if contracts - spec_docs:
            print(
                f"ERROR: task '{task_id}' lists contracts not in spec_docs: "
                f"{sorted(contracts - spec_docs)}."
            )
            ok = False

        non_md = sorted(p for p in spec_docs if not p.endswith(".md"))
        if non_md:
            print(
                f"ERROR: task '{task_id}' spec_docs must be OKF markdown concepts (.md): {non_md}."
            )
            ok = False

        reserved_contracts = okf.reserved_paths(contracts)
        if reserved_contracts:
            print(
                f"ERROR: task '{task_id}' contracts must be concept docs, not OKF reserved "
                f"files (index.md/log.md): {reserved_contracts}."
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
