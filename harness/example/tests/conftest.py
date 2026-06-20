"""Pytest bootstrap: expose the flat sample-app modules on sys.path.

The sample app under ``example/src/`` is intentionally framework-free and flat
so the tests can import ``routes``/``models``/``queries`` directly, mirroring
how the orchestrator treats ``example/src/billing`` and ``example/src/db`` as
the agent's targets.
"""

from __future__ import annotations

import os
import sys

EXAMPLE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for _sub in ("src/billing", "src/db"):
    _path = os.path.join(EXAMPLE_ROOT, _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)
