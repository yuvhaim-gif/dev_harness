#!/usr/bin/env python3
"""Pre-commit gate: every declared spec_doc must be an OKF concept document.

Validates OKF v0.1 conformance (a non-empty ``type`` in the YAML frontmatter of
each concept, reserved-file rules for ``index.md``/``log.md``) plus the harness
rule that contract concepts carry no volatile ``timestamp``. This keeps the
information layer the harness reasons over self-describing and machine-readable.

Honours the human override (``SKIP_AGENT_HARNESS``) so a developer can perform
sweeping restructures locally; the server-side ``ci_enforce`` re-check ignores
that switch, so the corpus is still guaranteed conformant on the trusted runner.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import okf  # noqa: E402
from lock_policy import human_override_active  # noqa: E402


def main() -> int:
    if human_override_active():
        print("SKIP_AGENT_HARNESS set: human override -- OKF info-layer gate bypassed.")
        return 0

    ledger_path = sys.argv[1] if len(sys.argv) > 1 else "AGENTS.md"
    if not os.path.exists(ledger_path):
        print(f"ERROR: Missing operational ledger: {ledger_path}")
        return 1

    try:
        problems = okf.verify(ledger_path)
    except Exception as exc:  # noqa: BLE001 - a bad ledger must abort cleanly
        print(f"ERROR: could not validate OKF info layer: {exc}")
        return 1

    if problems:
        print("ERROR: OKF info-layer conformance failed:")
        for problem in problems:
            print(f"  - {problem}")
        print("Every spec_doc is an OKF concept (non-empty 'type'); contracts have no timestamp.")
        return 1

    print("OK: all declared spec_docs are OKF-conformant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
