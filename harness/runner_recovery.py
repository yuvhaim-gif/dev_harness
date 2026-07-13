"""Rollback, forensics, circuit-breakers, and the autorepair step."""

from __future__ import annotations

import gc
import os

import forensic
import git
import journal
import leases
import log_condenser
import prompt_builder
import state_sync
from lock_policy import (
    compute_allowlist,
    env_flag,
    is_coordination_path,
    is_valid_coordination_payload,
)
from runner_core import (
    BUDGET_ABORT_EXIT,
    CONTAINMENT_ABORT_EXIT,
    REPAIR_PROMPT_FILE,
    RunContext,
    _commit_coordination,
    _posix,
    _repo_dir,
    _shared_state_enabled,
    log,
)
from runner_llm import _run_llm


def _release_lease(ctx: RunContext, commit: bool) -> None:
    if ctx.dry_run or not ctx.lease_acquired:
        return
    path = leases.lease_path(ctx.task.task_id)
    leases.release(ctx.task.task_id)
    ctx.lease_acquired = False
    if commit:
        _commit_coordination(ctx, path, "release lease")
    if _shared_state_enabled(ctx):
        ok_pub = state_sync.publish_files(
            _repo_dir(ctx),
            {_posix(path): None},
            message=f"harness: release lease {ctx.task.task_id} [{ctx.agent_id}]",
        )
        if not ok_pub:
            # A dropped release is the worst silent failure: it can strand the
            # task behind a live lease until the TTL expires. Make it loud.
            warning = (
                f"FAILED to publish lease RELEASE for '{ctx.task.task_id}' to the "
                "shared ref; the task may appear leased to other agents until the "
                f"{leases.DEFAULT_TTL_SECONDS}s TTL expires. Re-run or clear the "
                "shared lease manually."
            )
            log(f"WARNING: {warning}")
            ctx.git_warnings.append(warning)


def _persist_journal(ctx: RunContext, outcome: str, notes: str = "") -> None:
    if ctx.dry_run or not ctx.journal_entry:
        return
    journal.finalize(ctx.journal_entry, outcome, notes)
    path = journal.write(ctx.journal_entry)
    _commit_coordination(ctx, path, f"journal {outcome}")
    log(f"handover journal written ({outcome}): {path}")
    if _shared_state_enabled(ctx):
        posix_path = _posix(path)
        ok_pub = state_sync.publish_files(
            _repo_dir(ctx),
            {posix_path: posix_path},
            message=f"harness: journal {outcome} {ctx.task.task_id} [{ctx.agent_id}]",
        )
        # Record durability: only once the journal is mirrored off the work
        # branch (onto the shared ref) is it safe for rollback to delete that
        # branch. A local-only journal lives *only* on the work branch, so a
        # failed publish (or minimal / no-origin mode) must keep the branch.
        ctx.journal_published = ok_pub
        if not ok_pub:
            warning = (
                f"could not publish '{outcome}' handover journal to the shared ref; "
                "a fresh clone may not recover this session's context."
            )
            log(f"WARNING: {warning}")
            ctx.git_warnings.append(warning)


# Paths under .harness/ the orchestrator itself writes and must survive rollback:
# the forensic logs, the telemetry sink, and the hashed contract manifest. Valid
# lease/journal artifacts are kept via the coordination-payload check below.
_HARNESS_MANAGED_PREFIXES: tuple[str, ...] = (".harness/logs/", ".harness/telemetry/")


_HARNESS_MANAGED_FILES: frozenset[str] = frozenset({".harness/contracts.lock"})


def _is_harness_managed(repo_dir: str, path: str) -> bool:
    """True when an untracked ``.harness/`` path is the harness's own artifact.

    Forensic logs, telemetry, and the manifest are kept. A coordination path is
    kept only when its on-disk content is a well-formed lease/journal artifact;
    LLM-written junk under .harness/ (a stray .py, malformed JSON, or files
    outside the managed subtrees) is *not* managed and is removed by rollback.
    """
    norm = path.replace("\\", "/")
    if norm in _HARNESS_MANAGED_FILES:
        return True
    if any(norm.startswith(p) for p in _HARNESS_MANAGED_PREFIXES):
        return True
    if is_coordination_path(norm):
        try:
            with open(os.path.join(repo_dir, norm), encoding="utf-8") as fh:
                blob: str | None = fh.read()
        except OSError:
            blob = None
        return is_valid_coordination_payload(norm, blob)
    return False


def _cleanup_work_branch(ctx: RunContext) -> None:
    """Delete the rolled-back work branch once its journal is safely off-branch.

    Called only after the checkout back to the original branch succeeded (so the
    branch is no longer checked out and can be force-deleted). Force (`-D`) is
    correct: the branch holds the runner's own commits that the rollback is
    discarding by design.

    Gated on ``journal_published``: the handover journal is committed *on the
    work branch*, so deleting the branch is only safe once that journal has been
    mirrored to the shared state ref. In minimal / no-origin mode, or after a
    failed publish, the branch is the sole local record -- keep it and log its
    name for manual pruning. A delete failure is non-fatal (rollback already
    succeeded); it is recorded as a warning, not an abort.
    """
    if not ctx.work_branch:
        return
    if not ctx.journal_published:
        log(
            f"retained work branch '{ctx.work_branch}' (handover journal not "
            "mirrored to the shared ref); prune manually after inspection."
        )
        return
    try:
        ctx.repo.git.branch("-D", ctx.work_branch)
        log(f"deleted rolled-back work branch '{ctx.work_branch}'.")
    except git.exc.GitCommandError as exc:
        log(f"WARNING: could not delete work branch '{ctx.work_branch}': {exc}")
        ctx.git_warnings.append(f"work-branch cleanup failed: {exc}")


def _rollback(ctx: RunContext) -> None:
    if ctx.dry_run or not ctx.branch_created or not ctx.original_branch:
        return
    _release_lease(ctx, commit=False)
    reset_ok = False
    try:
        # Clear any uncommitted/staged mutation the blocked commit left behind
        # so the working tree is pristine before we leave the work branch.
        ctx.repo.git.reset("--hard")
        repo_dir = _repo_dir(ctx)
        listed = ctx.repo.git.ls_files("--others", "--exclude-standard")
        # Remove every agent-created untracked file -- including junk the LLM
        # wrote under .harness/ -- but keep the harness's own coordination state,
        # forensic logs, telemetry, and manifest so the audit stays intact.
        new_untracked = [
            p
            for p in listed.splitlines()
            if p and p not in ctx.baseline_untracked and not _is_harness_managed(repo_dir, p)
        ]
        for rel in new_untracked:
            try:
                os.remove(os.path.join(repo_dir, rel))
            except OSError:
                pass
        reset_ok = True
    except git.exc.GitCommandError as exc:
        log(f"WARNING: hard reset during rollback failed: {exc}")
        ctx.git_warnings.append(f"hard reset failed: {exc}")
    last_exc: git.exc.GitCommandError | None = None
    for attempt in range(2):
        try:
            ctx.repo.git.checkout(ctx.original_branch)
            log(f"rolled back to original branch '{ctx.original_branch}'.")
            _cleanup_work_branch(ctx)
            ctx.rollback_ok = reset_ok
            return
        except git.exc.GitCommandError as exc:
            last_exc = exc
            if attempt == 0:
                # A lingering GitPython/Windows file handle can pin the work
                # tree; drop caches and retry once before giving up.
                gc.collect()
    # Fail loud: leaving the workspace on the breach branch is an operational
    # hazard, so record it unambiguously (not a swallowed warning) with the
    # manual recovery step. Containment (exit 4) is unaffected -- that verdict
    # is based on committed state, not on where the local HEAD ends up.
    ctx.rollback_ok = False
    log(
        "ERROR: rollback checkout FAILED; workspace may be stranded on the work "
        f"branch. Recover manually with: git checkout {ctx.original_branch} ({last_exc})"
    )
    ctx.git_warnings.append(f"rollback checkout failed (workspace may be stranded): {last_exc}")


def _modified_paths(ctx: RunContext) -> list[str]:
    """Repo-relative paths changed since the base commit (committed + working)."""
    if ctx.dry_run:
        return []
    seen: set[str] = set()
    try:
        if ctx.base_commit:
            committed = ctx.repo.git.diff("--name-only", ctx.base_commit)
            seen.update(p for p in committed.splitlines() if p)
        working = ctx.repo.git.diff("--name-only")
        seen.update(p for p in working.splitlines() if p)
        staged = ctx.repo.git.diff("--cached", "--name-only")
        seen.update(p for p in staged.splitlines() if p)
        untracked = ctx.repo.git.ls_files("--others", "--exclude-standard")
        seen.update(p for p in untracked.splitlines() if p)
    except git.exc.GitCommandError as exc:
        log(f"WARNING: could not enumerate modified paths: {exc}")
    return sorted(seen)


def _committed_out_of_scope(ctx: RunContext, allow: list[str]) -> list[str]:
    """Out-of-allowlist paths in *committed* history (``base..HEAD``).

    Mirrors the authoritative containment gate
    (``runner_containment._containment_breach``), which inspects committed state
    only and deliberately ignores uncommitted scratch files. Keeping the forensic
    breach list on the same footing stops a benign untracked file from being
    mislabeled a "containment breach attempt" on an escalation unrelated to scope.
    """
    if ctx.dry_run or not ctx.base_commit:
        return []
    try:
        out = ctx.repo.git.diff("--name-only", f"{ctx.base_commit}..HEAD")
    except git.exc.GitCommandError as exc:
        log(f"WARNING: could not enumerate committed paths: {exc}")
        return []
    committed = [p for p in out.splitlines() if p]
    return sorted(p for p in committed if p not in allow and not is_coordination_path(p))


def _capture_work_diff(ctx: RunContext) -> tuple[str, str, str]:
    """Snapshot the work-branch delta vs. base *before* rollback deletes it.

    Returns ``(tip_short_sha, diffstat, full_patch)``. The forensic report is
    built while the work branch is still ``HEAD``, so this preserves exactly what
    the agent tried -- committed and uncommitted tracked changes alike -- as
    durable text under ``.harness/logs/``. That is why the later ``git branch -D``
    never costs an operator the diff: the record does not depend on the dangling
    commit surviving the host's next ``git gc``. A git failure is non-fatal: it
    degrades to an empty capture plus a warning, never blocking the abort.
    """
    if ctx.dry_run or not ctx.base_commit:
        return ("", "", "")
    try:
        sha = ctx.repo.git.rev_parse("--short", "HEAD")
        diffstat = ctx.repo.git.diff("--stat", ctx.base_commit)
        patch = ctx.repo.git.diff(ctx.base_commit)
    except git.exc.GitCommandError as exc:
        warning = f"could not capture work-branch diff for forensics: {exc}"
        log(f"WARNING: {warning}")
        ctx.git_warnings.append(warning)
        return ("", "", "")
    return (sha.strip(), diffstat.strip(), patch)


def _build_forensic_report(
    ctx: RunContext, outcome: str, reason: str, exit_code: int | None
) -> forensic.ForensicReport | None:
    """Compile the post-mortem audit against the *current* (pre-rollback) tree.

    The scope evidence in sections 1-3 (allowlist vs. modified paths, the
    containment proof) must be read *before* ``_rollback`` wipes the working
    tree, so this is called first at every abort site. The ``rollback_ok`` /
    ``git_warnings`` fields are only provisional here; ``_emit_forensic_report``
    refreshes them after the rollback actually runs so section 4 tells the truth.

    Returns ``None`` when forensics are suppressed (dry run) or already emitted,
    so callers can pass the result straight to ``_emit_forensic_report``.
    """
    if ctx.dry_run or ctx.forensic_written:
        return None
    allow = sorted(compute_allowlist(ctx.task.raw))
    modified = _modified_paths(ctx)
    out_of_scope = _committed_out_of_scope(ctx, allow)
    excerpt = log_condenser.condense(ctx.last_hook_log, repo_dir=_repo_dir(ctx))
    work_commit, work_diffstat, work_patch = _capture_work_diff(ctx)
    ctx.work_patch = work_patch
    return forensic.ForensicReport(
        task_id=ctx.task.task_id,
        mutation_mode=ctx.task.mutation_mode,
        outcome=outcome,
        reason=reason,
        base_commit=ctx.base_commit,
        work_branch=ctx.work_branch,
        work_commit=work_commit,
        work_diffstat=work_diffstat,
        error_code=exit_code,
        allowed=allow,
        modified=modified,
        out_of_scope=out_of_scope,
        failure_excerpt=excerpt or ctx.last_hook_log,
        git_warnings=list(ctx.git_warnings),
        attempts=list(ctx.journal_entry.get("attempts", [])),
        telemetry=ctx.ledger.as_dict(),
        rollback_ok=ctx.rollback_ok,
        env_scope="allowlisted" if os.getenv("AGENT_ENV_ALLOWLIST") else "full_copy",
    )


def _emit_forensic_report(ctx: RunContext, report: forensic.ForensicReport | None) -> None:
    """Write the audit and print the containment badge exactly once.

    Refreshes the rollback-dependent fields from the context at emit time, so a
    report *built* before ``_rollback`` still reports the real rollback verdict
    (and any warnings the rollback itself raised) rather than the stale default.
    """
    if report is None or ctx.dry_run or ctx.forensic_written:
        return
    report.rollback_ok = ctx.rollback_ok
    report.git_warnings = list(ctx.git_warnings)
    repo_dir = _repo_dir(ctx)
    path = forensic.write_report(report, repo_dir=repo_dir)
    # Also persist the run to the durable OKF memory layer (an OKF Postmortem
    # concept + a dated log.md entry). These live under the harness-managed,
    # rollback-surviving .harness/logs/ tree, so they are never wiped by the
    # abort's hard reset and never collide with the task allowlist.
    try:
        forensic.write_okf_postmortem(report, repo_dir=repo_dir)
        forensic.append_log(report, repo_dir=repo_dir)
        # Preserve the agent's attempted diff before the work branch is deleted,
        # so escalated runs stay inspectable without racing the host's git gc.
        if ctx.work_patch:
            patch_path = forensic.write_work_patch(report, ctx.work_patch, repo_dir=repo_dir)
            if patch_path:
                log(f"captured work-branch diff for forensics: {patch_path}")
    except OSError as exc:
        log(f"WARNING: could not write OKF memory artifacts: {exc}")
    forensic.print_badge(report.outcome, path)
    ctx.forensic_written = True


def _write_forensics(ctx: RunContext, outcome: str, reason: str, exit_code: int | None) -> None:
    """Build and emit the forensic report in a single step.

    Retained as a convenience wrapper for callers that do not roll back (or that
    roll back *before* reporting). Abort paths that roll back afterwards must
    instead call ``_build_forensic_report`` before ``_rollback`` and
    ``_emit_forensic_report`` after it, so section 4 reflects the real outcome.
    """
    _emit_forensic_report(ctx, _build_forensic_report(ctx, outcome, reason, exit_code))


def _abort_with_forensics(
    ctx: RunContext,
    *,
    reason: str,
    notes: str,
    exit_code: int,
    warning: str | None = None,
    outcome: str = "escalated",
) -> None:
    """Shared abort tail for the circuit-breakers and the autorepair cap.

    Builds the forensic report against the *current* (pre-rollback) tree, finalizes
    the journal, hard-rolls-back, then emits the report -- the build-before /
    emit-after ordering that lets section 4 reflect the real rollback verdict.
    When ``warning`` is given it is appended to ``git_warnings`` first.
    """
    if warning is not None:
        ctx.git_warnings.append(warning)
    report = _build_forensic_report(ctx, outcome, reason, exit_code=exit_code)
    _persist_journal(ctx, outcome, notes=notes)
    _rollback(ctx)
    _emit_forensic_report(ctx, report)


def autorepair(ctx: RunContext) -> bool:
    """Return True to retry the loop, False to escalate (caller should stop)."""
    journal.record_attempt(
        ctx.journal_entry, "enforce", ctx.last_status or "semantic", ctx.last_hook_log
    )
    ctx.autorepair_attempts += 1
    if ctx.autorepair_attempts > ctx.task.max_autorepair_attempts:
        log(
            "escalating: exceeded max_autorepair_attempts "
            f"({ctx.task.max_autorepair_attempts}); rolling back."
        )
        reason = (
            f"autorepair cap ({ctx.task.max_autorepair_attempts}) exceeded. "
            "Last hook log captured in the final attempt; the next agent "
            "should decide whether to fix the implementation or revise the "
            "test/contract that keeps failing."
        )
        _abort_with_forensics(ctx, reason=reason, notes=reason, exit_code=1)
        return False

    # Condense the raw hook log and build a cache-ordered repair prompt so the
    # fix loop stays lean and prompt-cache friendly.
    condensed = log_condenser.condense(ctx.last_hook_log, repo_dir=_repo_dir(ctx))
    prompt = prompt_builder.build_repair_prompt(
        task={**ctx.task.raw, "task_id": ctx.task.task_id},
        allowlist=sorted(compute_allowlist(ctx.task.raw)),
        condensed_log=condensed,
        attempt=ctx.autorepair_attempts,
        max_attempts=ctx.task.max_autorepair_attempts,
        diff=_work_diff(ctx),
        metrics=ctx.ledger.summary(),
    )
    prompt_file = prompt_builder.write_prompt(prompt, REPAIR_PROMPT_FILE)

    log(
        f"autorepair attempt {ctx.autorepair_attempts}/"
        f"{ctx.task.max_autorepair_attempts}: feeding condensed log to fix loop (LLM seam)."
    )
    _run_llm(ctx, "autorepair", repair_log=condensed, prompt_file=prompt_file)
    return True


def _work_diff(ctx: RunContext, max_chars: int = 6000) -> str:
    if ctx.dry_run:
        return ""
    try:
        base = ctx.base_commit or "HEAD"
        diff = str(ctx.repo.git.diff(base))
    except git.exc.GitCommandError:
        return ""
    return diff[:max_chars]


def _budget_abort(ctx: RunContext) -> bool:
    """Financial circuit-breaker: hard rollback + escalate if any budget breached."""
    reason = ctx.ledger.exceeded()
    if reason is None:
        return False
    log(f"FINANCIAL ABORT: {reason}; {ctx.ledger.summary()}. Rolling back.")
    _abort_with_forensics(
        ctx,
        warning=f"financial abort: {reason}",
        reason=f"financial abort -- {reason}",
        notes=f"financial abort: {reason}. {ctx.ledger.summary()}.",
        exit_code=BUDGET_ABORT_EXIT,
    )
    return True


def _timeout_abort(ctx: RunContext) -> bool:
    """Time circuit-breaker: hard rollback + escalate on step/wall-clock timeout.

    Shares BUDGET_ABORT_EXIT (3) so exit-code-based scripts keep working, but
    logs and stamps a *timeout* reason so an operator never mistakes a hung
    provider for a budget breach.
    """
    if not ctx.timed_out:
        return False
    log(f"TIMEOUT ABORT: {ctx.timed_out}; {ctx.ledger.summary()}. Rolling back.")
    _abort_with_forensics(
        ctx,
        warning=f"timeout abort: {ctx.timed_out}",
        reason=f"timeout abort -- {ctx.timed_out}",
        notes=f"timeout abort: {ctx.timed_out}. {ctx.ledger.summary()}.",
        exit_code=BUDGET_ABORT_EXIT,
    )
    return True


def _guard_ceiling(ctx: RunContext) -> int:
    """Guard-penalty ceiling: env override (>=1) else the autorepair budget."""
    raw = os.getenv("AGENT_GUARD_MAX_PENALTIES")
    if raw and raw.isdigit():
        return max(1, int(raw))
    return ctx.task.max_autorepair_attempts


def _guard_abort(ctx: RunContext) -> bool:
    """Containment circuit-breaker for repeated git-bypass attempts.

    Guard penalties have their own ceiling so a tamper-once agent keeps its full
    autorepair budget, but a persistent escape attempt is contained. Exits 4
    (the escape family), not 3, because this is a breach attempt, not a budget
    event. AGENT_GUARD_STRICT converts a single unstrippable (suspicious)
    pattern into an immediate abort.
    """
    hard = env_flag("AGENT_GUARD_STRICT") and ctx.guard_flagged >= 1
    if ctx.guard_penalties < _guard_ceiling(ctx) and not hard:
        return False
    reason = (
        f"repeated git-bypass attempts (penalties={ctx.guard_penalties}, "
        f"flagged={ctx.guard_flagged}, ceiling={_guard_ceiling(ctx)})"
    )
    log(f"GUARD ABORT: {reason}. Rolling back.")
    _abort_with_forensics(
        ctx,
        warning=f"guard abort: {reason}",
        reason=f"guard abort -- {reason}",
        notes=f"guard abort: {reason}.",
        exit_code=CONTAINMENT_ABORT_EXIT,
    )
    return True
