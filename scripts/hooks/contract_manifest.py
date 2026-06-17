#!/usr/bin/env python3
"""Hashed contract manifest: tie stable contracts to their on-disk content.

Each task in ``AGENTS.md`` may declare a ``contracts`` list. The sha256 of
every declared contract is recorded in ``.harness/contracts.lock``. A change to
a contract file that is *not* mirrored in the lock is, by definition, drift:
``verify()`` reports it and the contract tests fail. An *intended* contract
change updates the lock in the same commit (``evolve`` mode permits this), so
the manifest and the contract stay in lockstep.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable
from typing import Any

import yaml
from lock_policy import CONTRACT_LOCK_PATH

LOCK_VERSION = 1


def contract_paths_from_ledger(ledger: dict[str, Any]) -> set[str]:
    """Union of every task's declared ``contracts`` paths."""
    paths: set[str] = set()
    tasks = ledger.get("tasks") or {}
    for task in tasks.values():
        if isinstance(task, dict):
            paths |= set(task.get("contracts") or [])
    return paths


def sha256_of(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def compute_hashes(paths: Iterable[str]) -> dict[str, str]:
    return {p: sha256_of(p) for p in sorted(paths)}


def load_lock(path: str = CONTRACT_LOCK_PATH) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except FileNotFoundError:
        return {}
    contracts = data.get("contracts") if isinstance(data, dict) else None
    return dict(contracts) if isinstance(contracts, dict) else {}


def write_lock(hashes: dict[str, str], path: str = CONTRACT_LOCK_PATH) -> None:
    import os

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"version": LOCK_VERSION, "contracts": hashes}
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _load_ledger(ledger_path: str) -> dict[str, Any]:
    with open(ledger_path, encoding="utf-8") as fh:
        data: Any = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def verify(ledger_path: str = "AGENTS.md", lock_path: str = CONTRACT_LOCK_PATH) -> list[str]:
    """Return a list of human-readable mismatch messages (empty == OK)."""
    ledger = _load_ledger(ledger_path)
    declared = contract_paths_from_ledger(ledger)
    recorded = load_lock(lock_path)
    problems: list[str] = []

    for path in sorted(declared):
        try:
            live = sha256_of(path)
        except FileNotFoundError:
            problems.append(f"contract file missing on disk: {path}")
            continue
        if path not in recorded:
            problems.append(f"contract not recorded in manifest: {path}")
        elif recorded[path] != live:
            problems.append(f"contract drift (hash mismatch): {path}")

    for path in sorted(set(recorded) - declared):
        problems.append(f"manifest records an undeclared contract: {path}")

    return problems


def update(ledger_path: str = "AGENTS.md", lock_path: str = CONTRACT_LOCK_PATH) -> dict[str, str]:
    ledger = _load_ledger(ledger_path)
    hashes = compute_hashes(contract_paths_from_ledger(ledger))
    write_lock(hashes, lock_path)
    return hashes


def main() -> int:
    if "--update" in sys.argv[1:]:
        hashes = update()
        print(f"OK: wrote {CONTRACT_LOCK_PATH} ({len(hashes)} contract(s)).")
        return 0
    problems = verify()
    if problems:
        print("ERROR: contract manifest is out of date:")
        for p in problems:
            print(f"  - {p}")
        print("Run: python scripts/hooks/contract_manifest.py --update")
        return 1
    print(f"OK: {CONTRACT_LOCK_PATH} matches all declared contracts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
