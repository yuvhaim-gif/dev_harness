#!/usr/bin/env python3
"""Agent workflow orchestrator: the 5-state loop.

This module is a thin facade. The implementation lives in the ``runner_*``
sibling modules; everything below simply re-exports the public surface so
``python -m harness``, the ``harness.agent_runner:main`` console script, and the
test-suite imports keep working unchanged.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import journal  # noqa: E402, F401
from runner_cli import (  # noqa: E402, F401
    build_parser,
    doctor,
    init,
    list_tasks,
    main,
    release_lease,
    report_json,
)
from runner_core import (  # noqa: E402, F401
    BUDGET_ABORT_EXIT,
    CONTAINMENT_ABORT_EXIT,
    VERSION,
    RunContext,
    TaskSpec,
    _commit_env,
    _parse_task,
)
from runner_drive import DriveModel, run_drive  # noqa: E402, F401
from runner_reconcile import reconcile  # noqa: E402, F401
from runner_recovery import _release_lease, autorepair  # noqa: E402, F401
from runner_states import compute_branch_name, isolate  # noqa: E402, F401

if __name__ == "__main__":
    sys.exit(main())
