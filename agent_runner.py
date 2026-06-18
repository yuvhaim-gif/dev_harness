#!/usr/bin/env python3
"""Agent workflow orchestrator implementing the 5-state loop.

States: Initialize -> Isolate -> Mutate -> Enforce -> Autorepair/Reconcile.

The Mutate/Autorepair bodies are intentionally left as LLM integration seams;
everything around them (git isolation, scoped staging, lock enforcement,
classification, rollback, honest reconcile) is fully implemented and is the
part this framework hardens.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import git
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "hooks"))

import command_guard  # noqa: E402
import forensic  # noqa: E402
import journal  # noqa: E402
import leases  # noqa: E402
import log_condenser  # noqa: E402
import prompt_builder  # noqa: E402
import staleness  # noqa: E402
import state_sync  # noqa: E402
import telemetry  # noqa: E402
from lock_policy import compute_allowlist, is_coordination_path  # noqa: E402

SUPPORTED_SCHEMA_VERSION = 1

SHARED_REF = os.getenv("AGENT_SHARED_REF", "origin/main")

REPAIR_PROMPT_FILE = ".harness/telemetry/repair_prompt.txt"

BUDGET_ABORT_EXIT = 3


def log(msg: str) -> None:
    print(f"[agent_runner] {msg}")


# --------------------------------------------------------------------------- #
# E1. Data models
# --------------------------------------------------------------------------- #
@dataclass
class TaskSpec:
    task_id: str
    description: str
    mutation_mode: str
    spec_docs: list[str]
    tests: list[str]
    targets: list[str]
    locked_files: list[str]
    commit_prefix: str
    max_autorepair_attempts: int
    pr_labels: list[str]
    contracts: list[str]
    contract_tests: list[str]
    raw: dict[str, Any]


@dataclass
class RunContext:
    repo: git.Repo
    task: TaskSpec
    dry_run: bool
    agent_id: str = ""
    base_commit: str = ""
    original_branch: str = ""
    work_branch: str = ""
    autorepair_attempts: int = 0
    last_hook_log: str = ""
    branch_created: bool = field(default=False)
    lease_acquired: bool = field(default=False)
    handover_path: str = ""
    journal_entry: dict[str, Any] = field(default_factory=dict)
    ledger: telemetry.TokenLedger = field(default_factory=telemetry.TokenLedger)
    git_warnings: list[str] = field(default_factory=list)
    rollback_ok: bool = False
    forensic_written: bool = False


# --------------------------------------------------------------------------- #
# Ledger parsing
# --------------------------------------------------------------------------- #
def _load_ledger(path: str = "AGENTS.md") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit("ERROR: AGENTS.md must be a YAML mapping.")
    schema_version = data.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version > SUPPORTED_SCHEMA_VERSION:
        raise SystemExit(
            f"ERROR: unsupported schema_version {schema_version!r} "
            f"(this runner supports <= {SUPPORTED_SCHEMA_VERSION})."
        )
    return data


def _parse_task(task_id: str) -> TaskSpec:
    ledger = _load_ledger()
    tasks = ledger.get("tasks") or {}
    raw = tasks.get(task_id)
    if not isinstance(raw, dict):
        raise SystemExit(f"ERROR: task '{task_id}' not found in AGENTS.md.")
    return TaskSpec(
        task_id=task_id,
        description=str(raw.get("description", "")).strip(),
        mutation_mode=str(raw.get("mutation_mode", "")),
        spec_docs=list(raw.get("spec_docs") or []),
        tests=list(raw.get("tests") or []),
        targets=list(raw.get("targets") or []),
        locked_files=list(raw.get("locked_files") or []),
        commit_prefix=str(raw.get("commit_prefix", "chore")),
        max_autorepair_attempts=int(raw.get("max_autorepair_attempts", 3)),
        pr_labels=list(raw.get("pr_labels") or []),
        contracts=list(raw.get("contracts") or []),
        contract_tests=list(raw.get("contract_tests") or []),
        raw=raw,
    )


def _has_origin(repo: git.Repo) -> bool:
    return any(remote.name == "origin" for remote in repo.remotes)


def _repo_dir(ctx: RunContext) -> str:
    return str(ctx.repo.working_tree_dir or ".")


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _state_enabled(ctx: RunContext) -> bool:
    """Shared-ref coordination is live only for real runs against an origin."""
    return not ctx.dry_run and _has_origin(ctx.repo)


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
    shared = _shared_latest_unresolved(ctx) if _has_origin(ctx.repo) else None

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


# --------------------------------------------------------------------------- #
# E2. State 1 — Initialize
# --------------------------------------------------------------------------- #
def initialize(args: argparse.Namespace) -> RunContext:
    repo = git.Repo(search_parent_directories=True)

    if repo.is_dirty(untracked_files=False):
        raise SystemExit("ERROR: refusing to run on a dirty working tree.")

    tracking: Any | None = None
    try:
        tracking = repo.active_branch.tracking_branch()
    except (TypeError, ValueError):
        tracking = None

    if tracking is not None and _has_origin(repo):
        log("tracking remote detected; pulling from origin...")
        repo.remotes.origin.pull()
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
    )

    _recover_handover(ctx)

    log(f"initialized for task '{task.task_id}' (mode={task.mutation_mode}, agent={agent_id}).")
    return ctx


# --------------------------------------------------------------------------- #
# E3. State 2 — Isolate
# --------------------------------------------------------------------------- #
def compute_branch_name(task_id: str, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    # NOTE: strftime form is colon-free; isoformat() emits ':' and '+',
    # which git check-ref-format rejects.
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"agent/{task_id}/{stamp}"


def _commit_coordination(ctx: RunContext, path: str, what: str) -> None:
    """Commit harness-managed coordination state (lease/journal) on its own.

    The enforce hook permits coordination paths regardless of the active task,
    so this never collides with the work allowlist.
    """
    if ctx.dry_run:
        return
    posix = path.replace("\\", "/")
    env = os.environ.copy()
    env["AGENT_TASK_ID"] = ctx.task.task_id
    if os.path.exists(posix):
        ctx.repo.git.add("--", posix)
    else:
        ctx.repo.git.rm("--cached", "--ignore-unmatch", "--", posix)
    subprocess.run(
        ["git", "commit", "-m", f"chore(harness): {what} [{ctx.task.task_id}]"],
        capture_output=True,
        text=True,
        env=env,
    )


def _llm_env(
    ctx: RunContext, phase: str, repair_log: str = "", prompt_file: str = ""
) -> dict[str, str]:
    allow = sorted(compute_allowlist(ctx.task.raw))
    env = os.environ.copy()
    env.update(
        {
            "AGENT_TASK_ID": ctx.task.task_id,
            "AGENT_TASK_DESCRIPTION": ctx.task.description,
            "AGENT_MUTATION_MODE": ctx.task.mutation_mode,
            "AGENT_PHASE": phase,
            "AGENT_ALLOWLIST": "\n".join(allow),
            "AGENT_SPEC_DOCS": "\n".join(ctx.task.spec_docs),
            "AGENT_TESTS": "\n".join(ctx.task.tests),
            "AGENT_TARGETS": "\n".join(ctx.task.targets),
            "AGENT_CONTRACTS": "\n".join(ctx.task.contracts),
            "AGENT_CONTRACT_TESTS": "\n".join(ctx.task.contract_tests),
            "AGENT_HANDOVER_FILE": ctx.handover_path,
            "AGENT_REPAIR_LOG": repair_log,
            "AGENT_REPAIR_PROMPT_FILE": prompt_file,
            "AGENT_TOKEN_USAGE_FILE": telemetry.usage_file_path(),
        }
    )
    return env


def _run_llm(ctx: RunContext, phase: str, repair_log: str = "", prompt_file: str = "") -> bool:
    """Invoke the configured LLM command for ``phase``.

    Provider-agnostic: ``AGENT_LLM_CMD`` is any shell command. Before running, the
    command string is scanned for git bypass flags (``--no-verify``/``-n``); any
    found are stripped and a penalty is logged. Task context, the allowlist, the
    cache-ordered repair prompt, and the token-usage sink are passed through the
    environment. After the command, the per-step token/cost payload is read and
    accumulated. Returns True when a command ran, False when the seam is
    unconfigured (leaving the prior no-op behaviour intact for dry runs/tests).
    """
    cmd = os.getenv("AGENT_LLM_CMD")
    if not cmd:
        log(f"[{phase}] no AGENT_LLM_CMD set; LLM seam is a no-op.")
        return False

    guard = command_guard.sanitize_command(cmd)
    if guard.tampered:
        warning = (
            f"escape-hatch attempt in AGENT_LLM_CMD: stripped {guard.stripped} "
            f"(git bypass flags). Penalising repair counter."
        )
        log(f"[{phase}] PENALTY: {warning}")
        ctx.git_warnings.append(warning)
        ctx.autorepair_attempts += 1
    run_cmd = guard.sanitized

    usage_path = telemetry.usage_file_path()
    os.makedirs(os.path.dirname(usage_path) or ".", exist_ok=True)
    telemetry.clear_usage_file(usage_path)

    log(f"[{phase}] invoking AGENT_LLM_CMD (provider-agnostic seam).")
    res = subprocess.run(run_cmd, shell=True, env=_llm_env(ctx, phase, repair_log, prompt_file))
    if res.returncode != 0:
        log(f"[{phase}] WARNING: AGENT_LLM_CMD exited {res.returncode}.")

    step = ctx.ledger.record_from_file(phase, usage_path)
    if step is not None:
        log(f"[{phase}] telemetry: {ctx.ledger.summary()}")
    return True


def isolate(ctx: RunContext) -> None:
    repo = ctx.repo
    ctx.original_branch = repo.active_branch.name

    name = compute_branch_name(ctx.task.task_id)
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

    repo.git.checkout("-b", name)
    ctx.branch_created = True
    log(f"created and checked out work branch '{name}'.")

    if _state_enabled(ctx):
        shared_lease_path = _posix(leases.lease_path(ctx.task.task_id))
        remote_lease = state_sync.read_json(_repo_dir(ctx), shared_lease_path)
        if (
            remote_lease is not None
            and leases.is_active(remote_lease)
            and remote_lease.get("agent_id") != ctx.agent_id
        ):
            _rollback(ctx)
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
        _rollback(ctx)
        raise SystemExit(
            f"ERROR: task '{ctx.task.task_id}' is leased by "
            f"'{holder.get('agent_id')}' on '{holder.get('branch')}' "
            f"(created {holder.get('created_at')}). Back off or wait for it to expire."
        )
    ctx.lease_acquired = True
    lease_path = leases.lease_path(ctx.task.task_id)
    _commit_coordination(ctx, lease_path, "claim lease")
    if _state_enabled(ctx):
        posix_lease = _posix(lease_path)
        state_sync.publish_files(
            _repo_dir(ctx),
            {posix_lease: posix_lease},
            message=f"harness: claim lease {ctx.task.task_id} [{ctx.agent_id}]",
        )

    ctx.journal_entry = journal.start_session(ctx.task.task_id, name, ctx.base_commit)


# --------------------------------------------------------------------------- #
# E4. State 3 — Mutate (dispatch)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# E5. State 4 — Enforce
# --------------------------------------------------------------------------- #
def _staging_set(task: TaskSpec) -> list[str]:
    # Shared policy (B2) -> POSIX-normalized -> only paths that exist on disk.
    allow = sorted(compute_allowlist(task.raw))
    posix = [p.replace("\\", "/") for p in allow]
    return [p for p in posix if os.path.exists(p)]


def enforce(ctx: RunContext) -> tuple[str, str]:
    staging = _staging_set(ctx.task)

    if ctx.dry_run:
        log(f"[dry-run] would stage exactly: {staging or '(none)'}")
        log("[dry-run] skipping commit to keep 'no commits created' honest.")
        return ("dry-run", "")

    if staging:
        ctx.repo.git.add("--", *staging)

    env = os.environ.copy()
    env["AGENT_TASK_ID"] = ctx.task.task_id
    message = f"{ctx.task.commit_prefix}: {ctx.task.task_id}"
    res = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True,
        text=True,
        env=env,
    )
    out = (res.stdout or "") + (res.stderr or "")

    if res.returncode == 0:
        return ("passed", out)
    if "files were modified by this hook" in out.lower():
        return ("mechanical", out)
    return ("semantic", out)


# --------------------------------------------------------------------------- #
# E6. State 5A — Autorepair
# --------------------------------------------------------------------------- #
def _release_lease(ctx: RunContext, commit: bool) -> None:
    if ctx.dry_run or not ctx.lease_acquired:
        return
    path = leases.lease_path(ctx.task.task_id)
    leases.release(ctx.task.task_id)
    ctx.lease_acquired = False
    if commit:
        _commit_coordination(ctx, path, "release lease")
    if _has_origin(ctx.repo):
        state_sync.publish_files(
            _repo_dir(ctx),
            {_posix(path): None},
            message=f"harness: release lease {ctx.task.task_id} [{ctx.agent_id}]",
        )


def _persist_journal(ctx: RunContext, outcome: str, notes: str = "") -> None:
    if ctx.dry_run or not ctx.journal_entry:
        return
    journal.finalize(ctx.journal_entry, outcome, notes)
    path = journal.write(ctx.journal_entry)
    _commit_coordination(ctx, path, f"journal {outcome}")
    log(f"handover journal written ({outcome}): {path}")
    if _has_origin(ctx.repo):
        posix_path = _posix(path)
        state_sync.publish_files(
            _repo_dir(ctx),
            {posix_path: posix_path},
            message=f"harness: journal {outcome} {ctx.task.task_id} [{ctx.agent_id}]",
        )


def _rollback(ctx: RunContext) -> None:
    if ctx.dry_run or not ctx.branch_created or not ctx.original_branch:
        return
    _release_lease(ctx, commit=False)
    reset_ok = False
    try:
        # Clear any uncommitted/staged mutation the blocked commit left behind
        # so the working tree is pristine before we leave the work branch.
        ctx.repo.git.reset("--hard")
        ctx.repo.git.clean("-fd", "--", "src", "tests", "docs")
        reset_ok = True
    except git.exc.GitCommandError as exc:
        log(f"WARNING: hard reset during rollback failed: {exc}")
        ctx.git_warnings.append(f"hard reset failed: {exc}")
    try:
        ctx.repo.git.checkout(ctx.original_branch)
        log(f"rolled back to original branch '{ctx.original_branch}'.")
        ctx.rollback_ok = reset_ok
    except git.exc.GitCommandError as exc:
        log(f"WARNING: rollback checkout failed: {exc}")
        ctx.git_warnings.append(f"rollback checkout failed: {exc}")


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


def _write_forensics(ctx: RunContext, outcome: str, reason: str, exit_code: int | None) -> None:
    """Compile the post-mortem audit and print the containment badge once."""
    if ctx.dry_run or ctx.forensic_written:
        return
    allow = sorted(compute_allowlist(ctx.task.raw))
    modified = _modified_paths(ctx)
    out_of_scope = sorted(p for p in modified if p not in allow and not is_coordination_path(p))
    excerpt = log_condenser.condense(ctx.last_hook_log, repo_dir=_repo_dir(ctx))
    report = forensic.ForensicReport(
        task_id=ctx.task.task_id,
        mutation_mode=ctx.task.mutation_mode,
        outcome=outcome,
        reason=reason,
        base_commit=ctx.base_commit,
        work_branch=ctx.work_branch,
        error_code=exit_code,
        allowed=allow,
        modified=modified,
        out_of_scope=out_of_scope,
        failure_excerpt=excerpt or ctx.last_hook_log,
        git_warnings=list(ctx.git_warnings),
        attempts=list(ctx.journal_entry.get("attempts", [])),
        telemetry=ctx.ledger.as_dict(),
        rollback_ok=ctx.rollback_ok,
    )
    path = forensic.write_report(report, repo_dir=_repo_dir(ctx))
    forensic.print_badge(outcome, path)
    ctx.forensic_written = True


def autorepair(ctx: RunContext) -> bool:
    """Return True to retry the loop, False to escalate (caller should stop)."""
    journal.record_attempt(ctx.journal_entry, "enforce", "semantic", ctx.last_hook_log)
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
        _write_forensics(ctx, "escalated", reason, exit_code=1)
        _persist_journal(ctx, "escalated", notes=reason)
        _rollback(ctx)
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
    ctx.git_warnings.append(f"financial abort: {reason}")
    _write_forensics(ctx, "escalated", f"financial abort -- {reason}", exit_code=BUDGET_ABORT_EXIT)
    _persist_journal(ctx, "escalated", notes=f"financial abort: {reason}. {ctx.ledger.summary()}.")
    _rollback(ctx)
    return True


# --------------------------------------------------------------------------- #
# E7. State 5B — Reconcile
# --------------------------------------------------------------------------- #
def _ref_exists(repo: git.Repo, ref: str) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=repo.working_tree_dir,
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def _staleness_guard(ctx: RunContext) -> list[str]:
    """Fetch the shared ref and report critical files that moved since branch."""
    if not ctx.base_commit:
        return []
    try:
        ctx.repo.remotes.origin.fetch()
    except git.exc.GitError as exc:
        log(f"WARNING: fetch before staleness check failed: {exc}")
    working_dir = str(ctx.repo.working_tree_dir or ".")
    if not _ref_exists(ctx.repo, SHARED_REF):
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
        hint = "gh pr create --fill" + (f" --label {labels}" if labels else "")
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
    ctx.repo.git.push("-u", "origin", branch)
    log(f"pushed branch '{branch}' to origin.")
    _open_pr(ctx)
    return 0


# --------------------------------------------------------------------------- #
# E8. CLI / main loop
# --------------------------------------------------------------------------- #
def _drive(ctx: RunContext) -> int:
    while True:
        mutate(ctx)
        if _budget_abort(ctx):
            return BUDGET_ABORT_EXIT
        status, log_text = enforce(ctx)

        if status == "dry-run":
            return reconcile(ctx)

        if status == "mechanical":
            log("mechanical hook fix detected; re-staging and retrying once.")
            status, log_text = enforce(ctx)

        if status == "passed":
            return reconcile(ctx)

        # semantic (or still mechanical after the single retry) -> autorepair
        ctx.last_hook_log = log_text
        if not autorepair(ctx):
            return 1
        if _budget_abort(ctx):
            return BUDGET_ABORT_EXIT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent workflow orchestrator.")
    parser.add_argument(
        "--task",
        default=os.getenv("AGENT_TASK_ID"),
        help="Task id from AGENTS.md (defaults to $AGENT_TASK_ID).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only: compute branch/staging, never commit or push.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.task:
        print("ERROR: no task specified (use --task or set AGENT_TASK_ID).")
        return 2

    ctx = initialize(args)
    isolate(ctx)
    try:
        return _drive(ctx)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"unhandled error: {exc}; attempting rollback.")
        ctx.git_warnings.append(f"unhandled error: {exc}")
        _write_forensics(ctx, "error", f"Unhandled error: {exc}", exit_code=1)
        _persist_journal(ctx, "error", notes=f"Unhandled error: {exc}")
        _rollback(ctx)
        return 1


if __name__ == "__main__":
    sys.exit(main())
