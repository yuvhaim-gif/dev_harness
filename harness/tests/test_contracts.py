"""The contract<->test binding as a runnable check ("tests as compiler").

Every contract declared in AGENTS.md must hash-match .harness/contracts.lock.
An intended contract change updates the manifest in the same commit; an
unintended drift leaves the manifest behind and this test fails, forcing the
agent to decide whether to fix the change or revise the contract on purpose.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "harness"))

import contract_manifest  # noqa: E402


def test_contracts_match_manifest() -> None:
    ledger = os.path.join(REPO_ROOT, "AGENTS.md")
    lock = os.path.join(REPO_ROOT, ".harness", "contracts.lock")
    problems = contract_manifest.verify(ledger_path=ledger, lock_path=lock)
    assert problems == [], "contract drift:\n" + "\n".join(problems)
