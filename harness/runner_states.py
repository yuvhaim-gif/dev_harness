"""States 1-4: initialize, isolate, mutate, and enforce."""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import git
import journal
import leases
import state_sync
from lock_policy import compute_allowlist, human_override_active
from runner_core import (
    RunContext,
    TaskSpec,
    _commit_coordination,
    _commit_env,
    _has_origin,
    _parse_task,
    _posix,
    _record_runner_commit,
    _repo_dir,
    _shared_state_enabled,
    _state_enabled,
    log,
)
from runner_llm import _run_llm


def _shared_latest_unresolved(ctx: RunContext) -> dict[str, Any] | None:
    """Most recent unresolved journal for this task on the shared state ref."""
    repo_dir = _repo_dir(ctx)
    best: dict[str, Any] | None = None
    for path in state_sync.list_files(repo_dir, journal.JOURNAL_DIR):
        if not path.endswith(".json"):
            continue
        entry = state_sync.read_json(repo_dir, path)
        if entry is None or entry.get("task_id") != ctx.task.task_id:
            continue
        if entry.get("outcome") not in journal.UNRESOLVED_OUTCOMES:
            continue
        if best is None or str(entry.get("finished_at", "")) > str(best.get("finished_at", "")):
            best = entry
    return best


def _recover_handover(ctx: RunContext) -> None:
    """Pick up the latest unresolved session, local or from the shared ref.

    The shared ref makes handover survive a fresh clone: an abandoned session's
    journal is mirrored there, so a different machine can resume its context.
    """
    local = journal.latest_unresolved(ctx.task.task_id)
    shared = _shared_latest_unresolved(ctx) if _shared_state_enabled(ctx) else None

    chosen = local
    use_shared = shared is not None and (
        local is None or str(shared.get("finished_at", "")) > str(local.get("finished_at", ""))
    )
    if use_shared and shared is not None:
        chosen = shared
        if ctx.dry_run:
            ctx.handover_path = journal.session_path(str(shared.get("branch", "")))
        else:
            ctx.handover_path = journal.write(shared)
    elif chosen is not None:
        ctx.handover_path = journal.session_path(str(chosen.get("branch", "")))

    if chosen is not None:
        log(
            f"handover: resuming after unresolved session on "
            f"'{chosen.get('branch')}' (outcome={chosen.get('outcome')}); "
            f"context at {ctx.handover_path}."
        )


def initialize(args: argparse.Namespace) -> RunContext:
    repo = git.Repo(search_parent_directories=True)

    if repo.is_dirty(untracked_files=False):
        raise SystemExit("ERROR: refusing to run on a dirty working tree.")

    if human_override_active():
        log(
            "WARNING: SKIP_AGENT_HARNESS is set; it is a human-only override and "
            "will be ignored for this autonomous run's commits (gates stay active)."
        )

    tracking: Any | None = None
    try:
        tracking = repo.active_branch.tracking_branch()
    except (TypeError, ValueError):
        tracking = None

    if tracking is not None and _has_origin(repo):
        log("tracking remote detected; pulling from origin...")
        try:
            repo.remotes.origin.pull()
        except git.exc.GitCommandError as exc:
            log(f"WARNING: pull from origin failed; continuing on local state: {exc}")
    else:
        log("no tracking remote configured; skipping pull.")

    task = _parse_task(args.task)
    agent_id = os.getenv("AGENT_ID") or f"agent-{uuid.uuid4().hex[:8]}"
    try:
        base_commit = repo.head.commit.hexsha
    except (ValueError, git.exc.GitError):
        base_commit = ""

    ctx = RunContext(
        repo=repo,
        task=task,
        dry_run=bool(args.dry_run),
        agent_id=agent_id,
        base_commit=base_commit,
        start_time=time.monotonic(),
    )

    try:
        listed = repo.git.ls_files("--others", "--exclude-standard")
        ctx.baseline_untracked = frozenset(p for p in listed.splitlines() if p)
    except git.exc.GitCommandError:
        ctx.baseline_untracked = frozenset()

    _recover_handover(ctx)

    log(f"initialized for task '{task.task_id}' (mode={task.mutation_mode}, agent={agent_id}).")
    return ctx


def compute_branch_name(task_id: str, now: datetime | None = None, unique: bool = False) -> str:
    moment = now or datetime.now(UTC)
    # NOTE: strftime form is colon-free; isoformat() emits ':' and '+',
    # which git check-ref-format rejects.
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    if unique:
        stamp = f"{stamp}-{uuid.uuid4().hex[:6]}"
    return f"agent/{task_id}/{stamp}"


def isolate(ctx: RunContext) -> None:
    repo = ctx.repo
    ctx.original_branch = repo.active_branch.name

    # The branch name is pure to compute, so resolve and validate it before any
    # side effect. The uuid suffix keeps two same-second agents from colliding.
    name = compute_branch_name(ctx.task.task_id, unique=True)
    # Defensive pre-flight: raises GitCommandError on an invalid ref name.
    repo.git.check_ref_format("--branch", name)
    ctx.work_branch = name

    missing = [t for t in ctx.task.tests if not os.path.exists(t)]
    if ctx.task.mutation_mode == "isolated":
        missing += [t for t in ctx.task.targets if not os.path.exists(t)]
    if missing:
        raise SystemExit(f"ERROR: declared paths do not exist: {sorted(set(missing))}")

    if ctx.dry_run:
        log(f"[dry-run] computed work branch '{name}' (not created).")
        return

    # Lease gate FIRST: acquire the lease before `checkout -b` so a lost race
    # never creates an orphan work branch. Nothing branch-side has happened yet,
    # so an abort here needs no rollback. The uuid suffix above is
    # defense-in-depth on top of this ordering, not a substitute for it.
    if _state_enabled(ctx):
        shared_lease_path = _posix(leases.lease_path(ctx.task.task_id))
        remote_lease = state_sync.read_json(_repo_dir(ctx), shared_lease_path)
        if (
            remote_lease is not None
            and leases.is_active(remote_lease)
            and remote_lease.get("agent_id") != ctx.agent_id
        ):
            raise SystemExit(
                f"ERROR: task '{ctx.task.task_id}' is leased (shared ref) by "
                f"'{remote_lease.get('agent_id')}' on '{remote_lease.get('branch')}' "
                f"(created {remote_lease.get('created_at')}). "
                "Back off or wait for it to expire."
            )

    ok, holder = leases.acquire(
        task_id=ctx.task.task_id,
        branch=name,
        agent_id=ctx.agent_id,
        base_commit=ctx.base_commit,
        targets=ctx.task.targets,
    )
    if not ok and holder is not None:
        raise SystemExit(
            f"ERROR: task '{ctx.task.task_id}' is leased by "
            f"'{holder.get('agent_id')}' on '{holder.get('branch')}' "
            f"(created {holder.get('created_at')}). Back off or wait for it to expire."
        )
    ctx.lease_acquired = True

    # Only now create the branch; the untracked lease file (written by acquire
    # while still on the original branch) is carried into it by `checkout -b`.
    repo.git.checkout("-b", name)
    ctx.branch_created = True
    log(f"created and checked out work branch '{name}'.")

    lease_path = leases.lease_path(ctx.task.task_id)
    _commit_coordination(ctx, lease_path, "claim lease")
    if _state_enabled(ctx):
        posix_lease = _posix(lease_path)
        ok_pub = state_sync.publish_files(
            _repo_dir(ctx),
            {posix_lease: posix_lease},
            message=f"harness: claim lease {ctx.task.task_id} [{ctx.agent_id}]",
        )
        if not ok_pub:
            warning = (
                f"could not publish lease for '{ctx.task.task_id}' to the shared "
                "ref; other clones may not see this claim until it is retried."
            )
            log(f"WARNING: {warning}")
            ctx.git_warnings.append(warning)

    ctx.journal_entry = journal.start_session(ctx.task.task_id, name, ctx.base_commit)


def mutate(ctx: RunContext) -> None:
    mode = ctx.task.mutation_mode
    if mode == "evolve":
        log("[mutate] evolve: spec -> tests -> source (LLM integration seam).")
    elif mode == "isolated":
        log("[mutate] isolated: source-in-targets only (LLM integration seam).")
    else:
        raise SystemExit(f"ERROR: unknown mutation_mode '{mode}'.")

    if ctx.dry_run:
        return
    _run_llm(ctx, "mutate")


def _staging_set(task: TaskSpec) -> list[str]:
    # Shared policy (B2) -> POSIX-normalized -> only paths that exist on disk.
    allow = sorted(compute_allowlist(task.raw))
    posix = [p.replace("\\", "/") for p in allow]
    return [p for p in posix if os.path.exists(p)]


def _worktree_dirty(ctx: RunContext) -> bool:
    # Decide mechanical-vs-semantic by inspecting the worktree, not by parsing
    # English hook wording. If an earlier auto-fixer dirties the tree on the
    # same attempt a later hook blocks for a semantic reason, this misclassifies
    # that one attempt as mechanical; the wasted retry does not consume an
    # autorepair attempt and self-corrects next pass once the tree is clean.
    # That trade-off is deliberate -- do not "fix" it back to substring matching.
    return bool(ctx.repo.git.status("--porcelain").strip())


def enforce(ctx: RunContext) -> tuple[str, str]:
    staging = _staging_set(ctx.task)

    if ctx.dry_run:
        log(f"[dry-run] would stage exactly: {staging or '(none)'}")
        log("[dry-run] skipping commit to keep 'no commits created' honest.")
        return ("dry-run", "")

    if staging:
        ctx.repo.git.add("--", *staging)

    env = _commit_env(ctx)
    message = f"{ctx.task.commit_prefix}: {ctx.task.task_id}"
    res = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True,
        text=True,
        env=env,
    )
    out = (res.stdout or "") + (res.stderr or "")

    if res.returncode == 0:
        _record_runner_commit(ctx)
        return ("passed", out)
    if _worktree_dirty(ctx):
        return ("mechanical", out)
    return ("semantic", out)
