#!/usr/bin/env python3
"""Shared loader for the operational ledger (``AGENTS.md``).

Every gate and the orchestrator need to open ``AGENTS.md``, parse its YAML, and
look up a task mapping. Encoding that once here keeps the error wording and the
"must be a mapping" checks identical across the pre-commit hooks, the CI
re-check, and the runner, instead of drifting between copies.

Loaders raise :class:`LedgerError` on any problem (missing file, invalid YAML,
non-mapping top level); callers translate that into their own exit convention
(``sys.exit`` for hooks, ``SystemExit`` for the runner, ``return 1`` for
validators). Depends only on the standard library plus PyYAML, so it is safe to
import from a pre-commit hook running in its isolated ``pyyaml``-only venv.
"""

from __future__ import annotations

from typing import Any

import yaml


class LedgerError(Exception):
    """Raised when the ledger cannot be loaded or is structurally invalid."""


def load_ledger(path: str = "AGENTS.md") -> dict[str, Any]:
    """Open ``path`` and return its parsed YAML mapping.

    Raises :class:`LedgerError` when the file is missing, is not valid YAML, or
    does not parse to a mapping at the top level.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data: Any = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise LedgerError(f"Missing operational ledger: {path}") from exc
    except yaml.YAMLError as exc:
        raise LedgerError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LedgerError(f"{path} must be a YAML mapping at the top level.")
    return data


def get_task(ledger: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Return the mapping for ``task_id``, or ``None`` when absent/not a mapping."""
    task = (ledger.get("tasks") or {}).get(task_id)
    return task if isinstance(task, dict) else None
