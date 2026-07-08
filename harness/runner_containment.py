"""Post-hoc committed-state containment probes and the hard-stop abort."""

from __future__ import annotations

import git
import okf
from lock_policy import (
    compute_allowlist,
    is_coordination_path,
    is_valid_coordination_payload,
    symlink_paths,
)
from runner_core import CONTAINMENT_ABORT_EXIT, ContainmentCheckError, RunContext, log
from runner_recovery import _abort_with_forensics


def _unexpected_commits(ctx: RunContext) -> list[str]:
    """Commits on ``base..HEAD`` the orchestrator did not author itself.

    The pre-commit hook can be skipped by an agent that runs its own git
    (``-c core.hooksPath=...`` or plumbing), but any commit it creates still
    lands on the work branch. Anything here that is not in ``runner_commits``
    was written out of band -- a containment breach.
    """
    if ctx.dry_run or not ctx.base_commit:
        return []
    try:
        out = ctx.repo.git.rev_list(f"{ctx.base_commit}..HEAD")
    except git.exc.GitCommandError as exc:
        raise ContainmentCheckError(
            f"cannot list commits on {ctx.base_commit[:12]}..HEAD: {exc}"
        ) from exc
    shas = [s for s in out.splitlines() if s]
    return [s for s in shas if s not in ctx.runner_commits]


def _committed_paths(ctx: RunContext) -> list[str]:
    """Paths committed on ``base..HEAD`` (i.e. what a push would publish)."""
    if ctx.dry_run or not ctx.base_commit:
        return []
    try:
        out = ctx.repo.git.diff("--name-only", f"{ctx.base_commit}..HEAD")
    except git.exc.GitCommandError as exc:
        raise ContainmentCheckError(
            f"cannot diff {ctx.base_commit[:12]}..HEAD --name-only: {exc}"
        ) from exc
    return [p for p in out.splitlines() if p]


def _committed_blob(ctx: RunContext, path: str) -> str | None:
    """Content of ``path`` committed at HEAD; None when absent (e.g. deleted)."""
    try:
        return str(ctx.repo.git.show(f"HEAD:{path}"))
    except git.exc.GitCommandError:
        return None


def _committed_symlinks(ctx: RunContext) -> list[str]:
    """Symlinks committed on ``base..HEAD``, detected via git's recorded mode.

    A path inside the allowlist can be flipped to a symlink (mode 100644 ->
    120000) aimed at a locked file, which the path-only check cannot see.
    """
    if ctx.dry_run or not ctx.base_commit:
        return []
    try:
        raw = str(ctx.repo.git.diff("--raw", f"{ctx.base_commit}..HEAD"))
    except git.exc.GitCommandError as exc:
        raise ContainmentCheckError(
            f"cannot read --raw diff {ctx.base_commit[:12]}..HEAD: {exc}"
        ) from exc
    return symlink_paths(raw)


def _okf_violations(ctx: RunContext) -> list[str]:
    """OKF conformance breaches in spec_docs committed on ``base..HEAD``.

    The local ``validate-okf`` hook already fails a normal commit that breaks the
    info layer; this re-check inspects committed blobs so a malformed spec_doc
    that reached history via a hook-bypassing commit is still caught (exit 4).
    """
    if ctx.dry_run:
        return []
    spec = set(ctx.task.spec_docs)
    contracts = set(ctx.task.contracts)
    problems: list[str] = []
    for path in sorted(spec & set(_committed_paths(ctx))):
        blob = _committed_blob(ctx, path)
        if blob is None:
            continue
        problems.extend(okf.validate_concept_text(blob, path=path, is_contract=path in contracts))
    return problems


def _containment_breach(ctx: RunContext) -> list[str]:
    """Authoritative post-hoc check that the agent stayed inside its scope.

    Returns a list of human-readable violations (empty == contained). Inspects
    only *committed* state -- the history a push would publish -- so it catches
    out-of-allowlist commits and out-of-band (hook-bypassed) commits while
    ignoring benign uncommitted scratch files. This holds regardless of whether
    the pre-commit hook was skipped during the run.
    """
    if ctx.dry_run:
        return []
    # Fail closed: if any committed-state probe cannot run (git error), treat the
    # un-runnable check as a breach rather than a clean bill of health. This
    # mirrors the fail-safe patterns in ci_enforce._changed_files (exit 1) and
    # the strict staleness guard, so a deleted base object cannot silence the
    # gate. Deletions still map to _committed_blob -> None (benign), not an error.
    try:
        committed = _committed_paths(ctx)
        symlinks = _committed_symlinks(ctx)
        unexpected = _unexpected_commits(ctx)
        okf_bad = _okf_violations(ctx)
    except ContainmentCheckError as exc:
        return [f"containment check could not run (fail-closed): {exc}"]
    allow = sorted(compute_allowlist(ctx.task.raw))
    out_of_scope: list[str] = []
    bad_coord: list[str] = []
    for p in committed:
        if p in allow:
            continue
        if is_coordination_path(p):
            blob = _committed_blob(ctx, p)
            if blob is not None and not is_valid_coordination_payload(p, blob):
                bad_coord.append(p)
            continue
        out_of_scope.append(p)
    violations = [f"out-of-allowlist committed change: {p}" for p in sorted(out_of_scope)]
    violations += [f"invalid coordination payload committed: {p}" for p in sorted(bad_coord)]
    violations += [f"symlink committed (file-lock bypass): {p}" for p in sorted(symlinks)]
    violations += [f"out-of-band commit (hook-bypassed): {sha[:12]}" for sha in unexpected]
    violations += [f"OKF info-layer violation: {p}" for p in okf_bad]
    return violations


def _containment_abort(ctx: RunContext) -> bool:
    """Hard-stop if the agent escaped its declared scope despite the gates."""
    violations = _containment_breach(ctx)
    if not violations:
        return False
    log("CONTAINMENT BREACH: agent modified state outside its allowlist:")
    for v in violations:
        log(f"  - {v}")
    reason = "containment breach -- " + "; ".join(violations)
    _abort_with_forensics(
        ctx,
        warning=reason,
        reason=reason,
        notes=reason,
        exit_code=CONTAINMENT_ABORT_EXIT,
    )
    return True
