"""The contract<->test binding as a runnable check ("tests as compiler").

Every contract declared in AGENTS.md must hash-match .harness/contracts.lock.
An intended contract change updates the manifest in the same commit; an
unintended drift leaves the manifest behind and this test fails, forcing the
agent to decide whether to fix the change or revise the contract on purpose.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "harness"))

import contract_manifest  # noqa: E402


def test_contracts_match_manifest() -> None:
    ledger = os.path.join(REPO_ROOT, "AGENTS.md")
    lock = os.path.join(REPO_ROOT, ".harness", "contracts.lock")
    problems = contract_manifest.verify(ledger_path=ledger, lock_path=lock)
    assert problems == [], "contract drift:\n" + "\n".join(problems)


def test_hash_is_stable_and_covers_okf_frontmatter(tmp_path: Path) -> None:
    # The pinned hash is whole-file, so it includes the OKF frontmatter. Editing
    # a frontmatter field (e.g. a type rename) is therefore a contract change and
    # drifts the hash -- exactly why contracts forbid a volatile timestamp.
    contract = tmp_path / "API_SCHEMA.md"
    contract.write_text("---\ntype: API Contract\ntitle: X\n---\n\nbody\n", encoding="utf-8")
    baseline = contract_manifest.sha256_of(str(contract))
    assert contract_manifest.sha256_of(str(contract)) == baseline  # deterministic

    contract.write_text("---\ntype: Renamed Contract\ntitle: X\n---\n\nbody\n", encoding="utf-8")
    assert contract_manifest.sha256_of(str(contract)) != baseline
