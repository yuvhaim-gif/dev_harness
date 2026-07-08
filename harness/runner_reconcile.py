"""State 5B: staleness guard, push, PR, and the reconcile terminal."""

from __future__ import annotations

import shutil
import subprocess

import git
import staleness
from lock_policy import env_flag
from runner_core import SHARED_REF, RunContext, _has_origin, log
from runner_recovery import _persist_journal, _release_lease


def _ref_exists(repo: git.Repo, ref: str) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo.working_tree_dir,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def _is_shallow(repo: git.Repo) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=repo.working_tree_dir,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() == "true"


def _unshallow(repo: git.Repo) -> None:
    """Best-effort: a shallow clone lacks the base-commit objects the staleness
    guard must read, so deepen it before the critical-path comparison."""
    log("shallow clone detected; deepening history so staleness can be evaluated.")
    res = subprocess.run(
        ["git", "fetch", "--unshallow"],
        cwd=repo.working_tree_dir,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        log(f"WARNING: 'git fetch --unshallow' failed: {res.stderr.strip()}")


def _staleness_guard(ctx: RunContext) -> list[str]:
    """Fetch the shared ref and report critical files that moved since branch.

    On a shallow clone (common in CI) the base-commit objects may be absent,
    which would make a naive comparison silently pass. We deepen first, and when
    the shared ref still cannot be resolved we honour ``AGENT_STALENESS_STRICT``:
    strict mode blocks the push (fail-safe) rather than skipping the guard.
    """
    if not ctx.base_commit:
        return []
    if _is_shallow(ctx.repo):
        _unshallow(ctx.repo)
    try:
        ctx.repo.remotes.origin.fetch()
    except git.exc.GitError as exc:
        log(f"WARNING: fetch before staleness check failed: {exc}")
    working_dir = str(ctx.repo.working_tree_dir or ".")
    if not _ref_exists(ctx.repo, SHARED_REF):
        if env_flag("AGENT_STALENESS_STRICT"):
            log(
                f"shared ref '{SHARED_REF}' does not resolve and "
                "AGENT_STALENESS_STRICT is set; refusing to push (fail-safe)."
            )
            return [f"{SHARED_REF} (unresolvable; strict staleness)"]
        log(f"shared ref '{SHARED_REF}' does not resolve; skipping staleness check.")
        return []
    return staleness.check(working_dir, ctx.base_commit, SHARED_REF, ctx.task.raw)


def _open_pr(ctx: RunContext) -> None:
    labels = ",".join(ctx.task.pr_labels)
    if shutil.which("gh"):
        cmd = ["gh", "pr", "create", "--fill"]
        if labels:
            cmd += ["--label", labels]
        subprocess.run(cmd, check=False)
        log("requested PR creation via 'gh'.")
    else:
        # The real invocation above is a list (no shell), so this only guards the
        # log line: strip CR/LF from operator-supplied labels so an embedded
        # newline cannot spoof a fake '[agent_runner]' log record.
        safe_labels = labels.replace("\r", " ").replace("\n", " ")
        hint = "gh pr create --fill" + (f" --label {safe_labels}" if labels else "")
        log(f"GitHub CLI 'gh' not found. Manual PR: {hint}")


def reconcile(ctx: RunContext) -> int:
    branch = ctx.work_branch
    has_origin = _has_origin(ctx.repo)

    if ctx.dry_run:
        log(f"[dry-run] would run: git push -u origin {branch}")
        if has_origin:
            log("[dry-run] would open a PR via 'gh pr create' (if gh is available).")
        else:
            log(f"[dry-run] no 'origin' remote; manual push: git push -u origin {branch}")
        return 0

    if not has_origin:
        log(f"No 'origin' remote configured. Manual push: git push -u origin {branch}")
        _persist_journal(ctx, "local", notes="No origin; branch left for manual push.")
        _release_lease(ctx, commit=True)
        return 0

    stale = _staleness_guard(ctx)
    if stale:
        log("STALE: critical files moved on the shared ref since this branch started:")
        for path in stale:
            log(f"  - {path}")
        log("Refusing to push. Re-run after rebasing onto the updated contract.")
        _persist_journal(
            ctx,
            "stale",
            notes=(
                "Push blocked by the optimistic staleness guard. Contract/policy "
                f"files changed on {SHARED_REF} since base {ctx.base_commit[:12]}: "
                + ", ".join(stale)
                + ". The next agent must rebase and re-evaluate before retrying."
            ),
        )
        _release_lease(ctx, commit=False)
        return 1

    _persist_journal(ctx, "pushed", notes="Work committed and pushed; PR requested.")
    _release_lease(ctx, commit=True)
    try:
        ctx.repo.git.push("-u", "origin", branch)
    except git.exc.GitCommandError as exc:
        log(f"ERROR: push of '{branch}' to origin failed: {exc}")
        ctx.git_warnings.append(f"push failed: {exc}")
        _persist_journal(
            ctx,
            "error",
            notes=(
                f"Push to origin failed after a clean local run: {exc}. The lease "
                f"was released and the work remains committed locally on '{branch}'. "
                "Re-run the task to retry the push."
            ),
        )
        return 1
    log(f"pushed branch '{branch}' to origin.")
    _open_pr(ctx)
    return 0
