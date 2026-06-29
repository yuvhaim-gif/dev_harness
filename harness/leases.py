#!/usr/bin/env python3
"""Lightweight task leases: a claim so parallel agents do not collide.

Before mutating, an agent acquires a lease for its task under
``.harness/leases/<task_id>.json`` recording the owning agent, branch, base
commit, declared targets, and a TTL. A second agent that finds a live lease
held by someone else backs off; an expired lease may be reclaimed.

Leases are coordination state (committed by the orchestrator, never by the
LLM). True cross-agent visibility comes from pushing the lease to a shared
ref; the optimistic pre-push staleness guard is the backstop when two agents
raced on the same files regardless.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from typing import Any

LEASES_DIR = ".harness/leases"

DEFAULT_TTL_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def lease_path(task_id: str, leases_dir: str = LEASES_DIR) -> str:
    return os.path.join(leases_dir, f"{task_id}.json")


def read_lease(task_id: str, leases_dir: str = LEASES_DIR) -> dict[str, Any] | None:
    try:
        with open(lease_path(task_id, leases_dir), encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_active(lease: dict[str, Any], now: datetime | None = None) -> bool:
    moment = now or _now()
    created = _parse_stamp(str(lease.get("created_at", "")))
    ttl = int(lease.get("ttl_seconds", DEFAULT_TTL_SECONDS))
    if created is None:
        return False
    return (moment - created).total_seconds() < ttl


def acquire(
    task_id: str,
    branch: str,
    agent_id: str,
    base_commit: str,
    targets: list[str],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    leases_dir: str = LEASES_DIR,
) -> tuple[bool, dict[str, Any] | None]:
    """Try to claim ``task_id``.

    Returns ``(True, lease)`` on success, or ``(False, existing)`` when a live
    lease is already held by a different agent.
    """
    existing = read_lease(task_id, leases_dir)
    if existing is not None and is_active(existing) and existing.get("agent_id") != agent_id:
        return (False, existing)

    lease = {
        "task_id": task_id,
        "branch": branch,
        "agent_id": agent_id,
        "base_commit": base_commit,
        "targets": sorted(targets),
        "created_at": _stamp(_now()),
        "ttl_seconds": ttl_seconds,
    }
    os.makedirs(leases_dir, exist_ok=True)
    final = lease_path(task_id, leases_dir)

    # Fast path for a fresh claim: create the lease file exclusively so two
    # agents that both read "absent" cannot each believe they won. The loser of
    # the create race re-reads and backs off only if a *live other-agent* lease
    # now exists; an expired/own lease falls through to the atomic replace below.
    if existing is None:
        try:
            fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            other = read_lease(task_id, leases_dir)
            if other is not None and is_active(other) and other.get("agent_id") != agent_id:
                return (False, other)
            # expired / our own / raced-then-expired -> fall through to replace
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(lease, fh, indent=2, sort_keys=True)
                fh.write("\n")
            return (True, lease)

    # Reclaim path: write to a sibling temp file and os.replace into place. A
    # crash mid-write leaves the old lease (or nothing), never a half-written
    # file that read_lease would reject as corrupt and silently drop the claim.
    fd, tmp = tempfile.mkstemp(dir=leases_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(lease, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return (True, lease)


def release(task_id: str, leases_dir: str = LEASES_DIR) -> bool:
    path = lease_path(task_id, leases_dir)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
