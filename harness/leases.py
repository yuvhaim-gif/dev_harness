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
import re
import tempfile
import time
import uuid
from datetime import UTC, datetime
from typing import Any

LEASES_DIR = ".harness/leases"

# A task id is fused into filesystem paths (the lease/journal file name) and into
# the work-branch name. Constrain it to a conservative slug so it can never
# traverse out of ``.harness/leases`` / ``.harness/journal`` or smuggle path or
# ref metacharacters. Rejecting ``..`` outright is belt-and-braces on top of the
# character class (which already excludes ``/`` and ``\``).
_VALID_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def is_valid_task_id(task_id: str) -> bool:
    return (
        isinstance(task_id, str)
        and bool(_VALID_TASK_ID.fullmatch(task_id))
        and task_id not in {".", ".."}
        and ".." not in task_id
    )


DEFAULT_TTL_SECONDS = 3600

# A short-lived mutex (a directory, created atomically) serialises the reclaim of
# an expired lease so two processes that both read it as expired cannot each
# os.replace their own copy and both believe they won. A crashed holder leaves a
# stale mutex; it is stolen once older than this many seconds.
_RECLAIM_LOCK_STALE_SECONDS = 30

# On Windows, os.replace onto an existing file fails with a sharing violation
# (PermissionError) when another agent has the lease open for reading at that
# instant. The replace itself is still atomic; a few short retries ride out the
# transient sharing window so a contended-but-uncontested claim is not lost.
_REPLACE_RETRIES = 10
_REPLACE_BACKOFF_SECONDS = 0.01


def _atomic_replace(src: str, dst: str) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS)


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
    if not is_valid_task_id(task_id):
        raise ValueError(f"unsafe task_id for lease path: {task_id!r}")
    return os.path.join(leases_dir, f"{task_id}.json")


def read_lease(task_id: str, leases_dir: str = LEASES_DIR) -> dict[str, Any] | None:
    try:
        with open(lease_path(task_id, leases_dir), encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        # PermissionError: a concurrent atomic replace briefly holds the file on
        # Windows; treat it as momentarily absent rather than crashing the read.
        return None
    return data if isinstance(data, dict) else None


def is_active(lease: dict[str, Any], now: datetime | None = None) -> bool:
    moment = now or _now()
    created = _parse_stamp(str(lease.get("created_at", "")))
    if created is None:
        return False
    # A corrupted or adversarial lease (non-numeric ttl) must not crash the check
    # that the whole coordination layer depends on; treat it as inactive so a
    # legitimate agent can reclaim it rather than the run dying on a ValueError.
    try:
        ttl = int(lease.get("ttl_seconds", DEFAULT_TTL_SECONDS))
    except (ValueError, TypeError):
        return False
    return (moment - created).total_seconds() < ttl


def _acquire_reclaim_mutex(lock_dir: str) -> bool:
    """Atomically claim the reclaim critical section for one lease.

    ``os.mkdir`` is atomic and fails if the directory exists, so exactly one
    racer enters. A mutex left behind by a crashed holder is stolen once it is
    older than ``_RECLAIM_LOCK_STALE_SECONDS``. The stale takeover uses an
    atomic ``os.rename`` (not rmdir+mkdir, which is two syscalls and lets a
    second racer clobber the winner's fresh lock) so exactly one racer can move
    the stale directory aside, plus a second age check to avoid stealing a lock
    a racer recreated between our mtime read and the rename.
    """
    try:
        os.mkdir(lock_dir)
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock_dir)
        except OSError:
            return False
        if age <= _RECLAIM_LOCK_STALE_SECONDS:
            return False
        stealing = f"{lock_dir}.stealing-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            os.rename(lock_dir, stealing)
        except OSError:
            # Another racer already stole/cleared it; do not double-claim.
            return False
        try:
            fresh = time.time() - os.path.getmtime(stealing) <= _RECLAIM_LOCK_STALE_SECONDS
        except OSError:
            fresh = False
        if fresh:
            # A racer recreated the lock after our first mtime read; restore it
            # rather than steal a live mutex, then back off.
            try:
                os.rename(stealing, lock_dir)
            except OSError:
                pass
            return False
        try:
            os.rmdir(stealing)
        except OSError:
            pass
        try:
            os.mkdir(lock_dir)
            return True
        except OSError:
            return False


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

    # Reclaim path: serialise behind a mutex so two agents that both read the
    # lease as expired cannot each os.replace their own copy and both return
    # success (the TOCTOU race). The winner of the mutex re-reads under the lock
    # -- the lease may have just been reclaimed by a third agent or refreshed by
    # its live owner -- before committing the claim.
    lock_dir = final + ".lock"
    if not _acquire_reclaim_mutex(lock_dir):
        # Someone else is reclaiming right now; back off rather than double-claim.
        return (False, read_lease(task_id, leases_dir))
    try:
        current = read_lease(task_id, leases_dir)
        if current is not None and is_active(current) and current.get("agent_id") != agent_id:
            return (False, current)
        # Write to a sibling temp file and os.replace into place. A crash
        # mid-write leaves the old lease (or nothing), never a half-written file
        # that read_lease would reject as corrupt and silently drop the claim.
        fd, tmp = tempfile.mkstemp(dir=leases_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(lease, fh, indent=2, sort_keys=True)
                fh.write("\n")
            _atomic_replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return (True, lease)
    finally:
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass


def release(task_id: str, leases_dir: str = LEASES_DIR) -> bool:
    path = lease_path(task_id, leases_dir)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
