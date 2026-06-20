"""Entry point for ``python -m harness``.

Inserts this package directory on ``sys.path`` so the orchestrator's bare
sibling imports (``import command_guard`` etc.) resolve both in-place and when
the package is installed, then delegates to the orchestrator's ``main``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
