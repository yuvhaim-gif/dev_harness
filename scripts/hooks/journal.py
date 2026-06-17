#!/usr/bin/env python3
"""Append-only handover journal: continuity across agent sessions.

When a session ends -- success, escalation after the autorepair cap, or an
unhandled error -- a JSON record is written under ``.harness/journal/``. It
captures the task, branch, base commit, every (state, status) attempt, the
hook log excerpts that explained each failure, and a final outcome plus notes.

The next agent calls :func:`latest_unresolved` for the same task to recover
*what was tried and why it failed* before deciding how to proceed, so a rolled
-back or escalated session is never lost context.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

JOURNAL_DIR = ".harness/journal"

UNRESOLVED_OUTCOMES = frozenset({"escalated", "error", "stale"})

_LOG_EXCERPT_CHARS = 2000


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _branch_to_filename(branch: str) -> str:
    safe = branch.replace("/", "__").replace("\\", "__")
    return f"{safe}.json"


def session_path(branch: str, journal_dir: str = JOURNAL_DIR) -> str:
    return os.path.join(journal_dir, _branch_to_filename(branch))


def start_session(task_id: str, branch: str, base_commit: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "branch": branch,
        "base_commit": base_commit,
        "started_at": _now(),
        "attempts": [],
        "outcome": "in_progress",
        "notes": "",
        "finished_at": "",
    }


def record_attempt(entry: dict[str, Any], state: str, status: str, hook_log: str = "") -> None:
    attempts: list[dict[str, Any]] = entry.setdefault("attempts", [])
    attempts.append(
        {
            "at": _now(),
            "state": state,
            "status": status,
            "log_excerpt": (hook_log or "")[:_LOG_EXCERPT_CHARS],
        }
    )


def finalize(entry: dict[str, Any], outcome: str, notes: str = "") -> None:
    entry["outcome"] = outcome
    entry["finished_at"] = _now()
    if notes:
        entry["notes"] = notes


def write(entry: dict[str, Any], journal_dir: str = JOURNAL_DIR) -> str:
    os.makedirs(journal_dir, exist_ok=True)
    path = session_path(str(entry.get("branch", "session")), journal_dir)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(entry, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def _load(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def latest_unresolved(task_id: str, journal_dir: str = JOURNAL_DIR) -> dict[str, Any] | None:
    """Most recent journal entry for ``task_id`` that ended unresolved."""
    if not os.path.isdir(journal_dir):
        return None
    candidates: list[tuple[str, dict[str, Any]]] = []
    for name in os.listdir(journal_dir):
        if not name.endswith(".json"):
            continue
        entry = _load(os.path.join(journal_dir, name))
        if entry is None:
            continue
        if entry.get("task_id") == task_id and entry.get("outcome") in UNRESOLVED_OUTCOMES:
            candidates.append((str(entry.get("finished_at", "")), entry))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]
