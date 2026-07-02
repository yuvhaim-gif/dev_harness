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
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import git
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import command_guard  # noqa: E402
import contract_manifest  # noqa: E402
import forensic  # noqa: E402
import journal  # noqa: E402
import leases  # noqa: E402
import log_condenser  # noqa: E402
import okf  # noqa: E402
import prompt_builder  # noqa: E402
import staleness  # noqa: E402
import state_sync  # noqa: E402
import telemetry  # noqa: E402
from lock_policy import (  # noqa: E402
    compute_allowlist,
    human_override_active,
    is_coordination_path,
    is_valid_coordination_payload,
    symlink_paths,
)

SUPPORTED_SCHEMA_VERSION = 1

SHARED_REF = os.getenv("AGENT_SHARED_REF", "origin/main")

REPAIR_PROMPT_FILE = ".harness/telemetry/repair_prompt.txt"

BUDGET_ABORT_EXIT = 3
CONTAINMENT_ABORT_EXIT = 4

VERSION = "0.1.0"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Marker stamped into the template README the harness ships at the repo root.
# ``--doctor`` warns while it is still present so an operator replaces the
# framework's landing page with their own project README before starting.
README_SENTINEL = "<!-- HARNESS_TEMPLATE_README"

_EMPTY_LEDGER = "schema_version: 1\n\ntasks: {}\n"

_EXAMPLE_LEDGER_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "example", "AGENTS.example.md"
)

_PROJECT_README_TEMPLATE = """# Your Project

Built on the agent workflow harness. The framework and its full documentation
live under `harness/` (see `harness/README.md`). Define your tasks in
`AGENTS.md` and run them with:

```bash
python -m harness --task <task_id>
```
"""


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def _env_float(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _minimal_mode() -> bool:
    """Tier the optional coordination layer off for simple single-agent runs.

    ``AGENT_MINIMAL=1`` (or ``AGENT_DISABLE_STATE_SYNC=1``) keeps the core
    file-lock guarantee but skips the shared ``harness-state`` ref entirely, so
    operators who only want local locking are not exposed to the cross-clone
    git-plumbing machinery.
    """
    return _env_flag("AGENT_MINIMAL") or _env_flag("AGENT_DISABLE_STATE_SYNC")


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
    guard_penalties: int = 0
    last_hook_log: str = ""
    last_status: str = ""
    branch_created: bool = field(default=False)
    lease_acquired: bool = field(default=False)
    handover_path: str = ""
    journal_entry: dict[str, Any] = field(default_factory=dict)
    ledger: telemetry.TokenLedger = field(default_factory=telemetry.TokenLedger)
    git_warnings: list[str] = field(default_factory=list)
    rollback_ok: bool = False
    forensic_written: bool = False
    runner_commits: set[str] = field(default_factory=set)
    baseline_untracked: frozenset[str] = frozenset()
    env_warned: bool = False
    start_time: float = 0.0
    timed_out: str = ""


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
    attempts_raw = raw.get("max_autorepair_attempts", 3)
    if isinstance(attempts_raw, bool):
        raise SystemExit(
            f"ERROR: task '{task_id}' max_autorepair_attempts must be an integer, "
            f"got {attempts_raw!r}."
        )
    try:
        max_autorepair_attempts = int(attempts_raw)
    except (TypeError, ValueError):
        raise SystemExit(
            f"ERROR: task '{task_id}' max_autorepair_attempts must be an integer, "
            f"got {attempts_raw!r}."
        ) from None
    return TaskSpec(
        task_id=task_id,
        description=str(raw.get("description", "")).strip(),
        mutation_mode=str(raw.get("mutation_mode", "")),
        spec_docs=list(raw.get("spec_docs") or []),
        tests=list(raw.get("tests") or []),
        targets=list(raw.get("targets") or []),
        locked_files=list(raw.get("locked_files") or []),
        commit_prefix=str(raw.get("commit_prefix", "chore")),
        max_autorepair_attempts=max_autorepair_attempts,
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
    return not ctx.dry_run and _has_origin(ctx.repo) and not _minimal_mode()


def _shared_state_enabled(ctx: RunContext) -> bool:
    """Whether shared-ref publishing/reading should run for this context."""
    return _has_origin(ctx.repo) and not _minimal_mode()


def _record_runner_commit(ctx: RunContext) -> None:
    """Remember a commit the orchestrator itself created on the work branch.

    Anything on ``base..HEAD`` that is *not* in this set was authored out of
    band by the agent (e.g. via a hook-skipping commit) and is a containment
    breach -- the orchestrator is the only component permitted to write history.
    """
    try:
        ctx.runner_commits.add(ctx.repo.head.commit.hexsha)
    except (ValueError, git.exc.GitError):
        return


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


# --------------------------------------------------------------------------- #
# E2. State 1 — Initialize
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# E3. State 2 — Isolate
# --------------------------------------------------------------------------- #
def compute_branch_name(task_id: str, now: datetime | None = None, unique: bool = False) -> str:
    moment = now or datetime.now(UTC)
    # NOTE: strftime form is colon-free; isoformat() emits ':' and '+',
    # which git check-ref-format rejects.
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    if unique:
        stamp = f"{stamp}-{uuid.uuid4().hex[:6]}"
    return f"agent/{task_id}/{stamp}"


def _commit_env(ctx: RunContext) -> dict[str, str]:
    """Environment for the orchestrator's own git commits.

    Sets AGENT_TASK_ID so the lock/contract hooks gate the commit and drops
    SKIP_AGENT_HARNESS: that switch is a human-only override and must never
    disable the gates during an autonomous run (the LLM seam env drops it too).
    """
    env = os.environ.copy()
    env["AGENT_TASK_ID"] = ctx.task.task_id
    env.pop("SKIP_AGENT_HARNESS", None)
    return env


def _commit_coordination(ctx: RunContext, path: str, what: str) -> None:
    """Commit harness-managed coordination state (lease/journal) on its own.

    The enforce hook permits coordination paths regardless of the active task,
    so this never collides with the work allowlist.
    """
    if ctx.dry_run:
        return
    posix = path.replace("\\", "/")
    env = _commit_env(ctx)
    if os.path.exists(posix):
        ctx.repo.git.add("--", posix)
    else:
        ctx.repo.git.rm("--cached", "--ignore-unmatch", "--", posix)
    res = subprocess.run(
        ["git", "commit", "-m", f"chore(harness): {what} [{ctx.task.task_id}]"],
        capture_output=True,
        text=True,
        env=env,
    )
    if res.returncode == 0:
        _record_runner_commit(ctx)


def _harden_git_env(env: dict[str, str]) -> None:
    """Pin git config for the seam so the inherited environment cannot weaken it.

    This is defence-in-depth, not a sandbox: a command-line ``-c
    core.hooksPath=...`` still wins over configuration, which is exactly why the
    command guard flags it and the post-hoc containment gate + CI re-check are
    the authoritative boundaries. True isolation (no network, read-only ``.git``)
    requires running the seam in a container; see the README threat model.
    """
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_CONFIG_GLOBAL", None)


def _seam_base_env() -> dict[str, str]:
    """Base environment for the LLM subprocess.

    Default (no AGENT_ENV_ALLOWLIST) is a full copy, preserving setups that rely
    on inherited vars. When AGENT_ENV_ALLOWLIST is set (comma/newline-separated
    var names), start from only those vars plus the AGENT_*/GIT_* keys the
    harness manages, so the seam no longer inherits every secret in the parent
    environment. _harden_git_env still runs last in _llm_env.

    SKIP_AGENT_HARNESS is always dropped, even in full-copy mode: it is a
    human-only override that disables the local lock/contract hooks, and must
    never be inherited by git commands the agent itself spawns.
    """
    raw = os.getenv("AGENT_ENV_ALLOWLIST")
    if not raw:
        env = os.environ.copy()
    else:
        names = {n.strip() for n in raw.replace(",", "\n").splitlines() if n.strip()}
        env = {
            k: v
            for k, v in os.environ.items()
            if k in names or k.startswith("AGENT_") or k.startswith("GIT_")
        }
    env.pop("SKIP_AGENT_HARNESS", None)
    return env


def _llm_env(
    ctx: RunContext, phase: str, repair_log: str = "", prompt_file: str = ""
) -> dict[str, str]:
    allow = sorted(compute_allowlist(ctx.task.raw))
    env = _seam_base_env()
    _harden_git_env(env)
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

    if not os.getenv("AGENT_ENV_ALLOWLIST") and not ctx.env_warned:
        warning = (
            "AGENT_ENV_ALLOWLIST not set -- the LLM subprocess inherits the FULL "
            "parent environment, including any secrets. Set AGENT_ENV_ALLOWLIST to scope it."
        )
        log(f"[{phase}] WARNING: {warning}")
        ctx.git_warnings.append(warning)
        ctx.env_warned = True

    guard = command_guard.sanitize_command(cmd)
    if guard.tampered:
        warning = (
            f"escape-hatch attempt in AGENT_LLM_CMD: stripped {guard.stripped} "
            f"(git bypass flags). Charging the guard-penalty counter."
        )
        log(f"[{phase}] PENALTY: {warning}")
        ctx.git_warnings.append(warning)
        ctx.guard_penalties += 1
    if guard.suspicious:
        warning = (
            f"hook-evasion pattern in AGENT_LLM_CMD: {guard.flagged}. These cannot "
            "be stripped from an arbitrary shell command; the post-hoc containment "
            "gate and CI re-check are the backstop. Charging the guard-penalty counter."
        )
        log(f"[{phase}] PENALTY: {warning}")
        ctx.git_warnings.append(warning)
        ctx.guard_penalties += 1
    run_cmd = guard.sanitized

    usage_path = telemetry.usage_file_path()
    os.makedirs(os.path.dirname(usage_path) or ".", exist_ok=True)
    telemetry.clear_usage_file(usage_path)

    log(f"[{phase}] invoking AGENT_LLM_CMD (provider-agnostic seam).")
    step_timeout = _env_float("AGENT_STEP_TIMEOUT_SECONDS")
    # Put the seam shell in its own session / process group so a timeout can
    # kill the WHOLE tree. With shell=True, subprocess.run's own timeout only
    # SIGKILLs the immediate child (the shell); a forked grandchild
    # (sh -> bash -> sleep ...) would be orphaned and keep mutating the tree
    # after we have already rolled back. The tree is killed via killpg (POSIX)
    # / taskkill /T (Windows), which closes that hole on both platforms.
    popen_kwargs: dict[str, Any] = {
        "shell": True,
        "env": _llm_env(ctx, phase, repair_log, prompt_file),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(run_cmd, **popen_kwargs)
    try:
        proc.communicate(timeout=step_timeout)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            # taskkill /T tears down the whole child tree (CREATE_NEW_PROCESS_GROUP
            # gives us a clean group to target); proc.kill() is the fallback.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            except OSError:
                pass
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
        proc.communicate()
        ctx.timed_out = "step timeout (AGENT_STEP_TIMEOUT_SECONDS)"
        log(
            f"[{phase}] WARNING: AGENT_LLM_CMD exceeded AGENT_STEP_TIMEOUT_SECONDS; "
            "treating as a failed step."
        )
        return True
    if proc.returncode != 0:
        log(f"[{phase}] WARNING: AGENT_LLM_CMD exited {proc.returncode}.")

    # Wall-clock ceiling spanning every step of the run, not just this one.
    max_run = _env_float("MAX_RUN_SECONDS")
    if max_run is not None and (time.monotonic() - ctx.start_time) > max_run:
        ctx.timed_out = "wall-clock timeout (MAX_RUN_SECONDS)"

    step = ctx.ledger.record_from_file(phase, usage_path)
    if step is not None:
        log(f"[{phase}] telemetry: {ctx.ledger.summary()}")
    return True


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
    out_of_scope = sorted(p for p in modified if p not in allow and not is_coordination_path(p))
    excerpt = log_condenser.condense(ctx.last_hook_log, repo_dir=_repo_dir(ctx))
    return forensic.ForensicReport(
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
        report = _build_forensic_report(ctx, "escalated", reason, exit_code=1)
        _persist_journal(ctx, "escalated", notes=reason)
        _rollback(ctx)
        _emit_forensic_report(ctx, report)
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
    report = _build_forensic_report(
        ctx, "escalated", f"financial abort -- {reason}", exit_code=BUDGET_ABORT_EXIT
    )
    _persist_journal(ctx, "escalated", notes=f"financial abort: {reason}. {ctx.ledger.summary()}.")
    _rollback(ctx)
    _emit_forensic_report(ctx, report)
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
    ctx.git_warnings.append(f"timeout abort: {ctx.timed_out}")
    report = _build_forensic_report(
        ctx, "escalated", f"timeout abort -- {ctx.timed_out}", exit_code=BUDGET_ABORT_EXIT
    )
    _persist_journal(
        ctx, "escalated", notes=f"timeout abort: {ctx.timed_out}. {ctx.ledger.summary()}."
    )
    _rollback(ctx)
    _emit_forensic_report(ctx, report)
    return True


def _guard_abort(ctx: RunContext) -> bool:
    """Containment circuit-breaker for repeated git-bypass attempts.

    Guard penalties have their own ceiling so a tamper-once agent keeps its full
    autorepair budget, but a persistent escape attempt is contained. Exits 4
    (the escape family), not 3, because this is a breach attempt, not a budget
    event.
    """
    if ctx.guard_penalties < ctx.task.max_autorepair_attempts:
        return False
    reason = (
        f"repeated git-bypass attempts ({ctx.guard_penalties} >= "
        f"{ctx.task.max_autorepair_attempts})"
    )
    log(f"GUARD ABORT: {reason}. Rolling back.")
    ctx.git_warnings.append(f"guard abort: {reason}")
    report = _build_forensic_report(
        ctx, "escalated", f"guard abort -- {reason}", exit_code=CONTAINMENT_ABORT_EXIT
    )
    _persist_journal(ctx, "escalated", notes=f"guard abort: {reason}.")
    _rollback(ctx)
    _emit_forensic_report(ctx, report)
    return True


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
    except git.exc.GitCommandError:
        return []
    shas = [s for s in out.splitlines() if s]
    return [s for s in shas if s not in ctx.runner_commits]


def _committed_paths(ctx: RunContext) -> list[str]:
    """Paths committed on ``base..HEAD`` (i.e. what a push would publish)."""
    if ctx.dry_run or not ctx.base_commit:
        return []
    try:
        out = ctx.repo.git.diff("--name-only", f"{ctx.base_commit}..HEAD")
    except git.exc.GitCommandError:
        return []
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
    except git.exc.GitCommandError:
        return []
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
    allow = sorted(compute_allowlist(ctx.task.raw))
    out_of_scope: list[str] = []
    bad_coord: list[str] = []
    for p in _committed_paths(ctx):
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
    violations += [
        f"symlink committed (file-lock bypass): {p}" for p in sorted(_committed_symlinks(ctx))
    ]
    violations += [
        f"out-of-band commit (hook-bypassed): {sha[:12]}" for sha in _unexpected_commits(ctx)
    ]
    violations += [f"OKF info-layer violation: {p}" for p in _okf_violations(ctx)]
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
    ctx.git_warnings.append(reason)
    report = _build_forensic_report(ctx, "escalated", reason, exit_code=CONTAINMENT_ABORT_EXIT)
    _persist_journal(ctx, "escalated", notes=reason)
    _rollback(ctx)
    _emit_forensic_report(ctx, report)
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
        if _env_flag("AGENT_STALENESS_STRICT"):
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


# --------------------------------------------------------------------------- #
# E8. CLI / main loop
# --------------------------------------------------------------------------- #
# Abort checks fired right after a mutate step: budget and timeout exit 3,
# guard and post-hoc containment exit 4. Order is significant -- the first that
# trips wins, matching the original inline sequence.
_POST_MUTATE_ABORTS: tuple[tuple[Callable[[RunContext], bool], int], ...] = (
    (_budget_abort, BUDGET_ABORT_EXIT),
    (_timeout_abort, BUDGET_ABORT_EXIT),
    (_guard_abort, CONTAINMENT_ABORT_EXIT),
    (_containment_abort, CONTAINMENT_ABORT_EXIT),
)
# After autorepair nothing new is committed yet, so containment is not re-checked
# here -- it runs after the next iteration's enforce instead.
_POST_REPAIR_ABORTS: tuple[tuple[Callable[[RunContext], bool], int], ...] = (
    (_budget_abort, BUDGET_ABORT_EXIT),
    (_timeout_abort, BUDGET_ABORT_EXIT),
    (_guard_abort, CONTAINMENT_ABORT_EXIT),
)


@dataclass
class DriveModel:
    """Side-effecting steps and abort checks of the drive loop, injected so the
    transition logic can be unit-tested with fakes instead of subprocesses."""

    mutate: Callable[[RunContext], None]
    enforce: Callable[[RunContext], tuple[str, str]]
    autorepair: Callable[[RunContext], bool]
    reconcile: Callable[[RunContext], int]
    containment: Callable[[RunContext], bool]
    post_mutate_aborts: tuple[tuple[Callable[[RunContext], bool], int], ...]
    post_repair_aborts: tuple[tuple[Callable[[RunContext], bool], int], ...]


def _default_drive_model() -> DriveModel:
    return DriveModel(
        mutate=mutate,
        enforce=enforce,
        autorepair=autorepair,
        reconcile=reconcile,
        containment=_containment_abort,
        post_mutate_aborts=_POST_MUTATE_ABORTS,
        post_repair_aborts=_POST_REPAIR_ABORTS,
    )


def _first_abort(
    ctx: RunContext, checks: tuple[tuple[Callable[[RunContext], bool], int], ...]
) -> int | None:
    for check, code in checks:
        if check(ctx):
            return code
    return None


def run_drive(ctx: RunContext, model: DriveModel) -> int:
    """Run the mutate -> enforce -> autorepair/reconcile machine to a terminal code.

    Transitions per iteration:
      mutate -> (post-mutate aborts) -> enforce
        "dry-run"             -> reconcile (terminal)
        "mechanical"          -> enforce once more, then fall through
        "passed"              -> containment check, else reconcile (terminal)
        "semantic"/mechanical -> autorepair; cap exit 1, else (post-repair aborts), loop
    """
    while True:
        model.mutate(ctx)
        code = _first_abort(ctx, model.post_mutate_aborts)
        if code is not None:
            return code

        status, log_text = model.enforce(ctx)
        if status == "dry-run":
            return model.reconcile(ctx)
        if status == "mechanical":
            log("mechanical hook fix detected; re-staging and retrying once.")
            status, log_text = model.enforce(ctx)
        if status == "passed":
            if model.containment(ctx):
                return CONTAINMENT_ABORT_EXIT
            return model.reconcile(ctx)

        # semantic (or still mechanical after the single retry) -> autorepair
        ctx.last_hook_log = log_text
        ctx.last_status = status
        if not model.autorepair(ctx):
            return 1
        code = _first_abort(ctx, model.post_repair_aborts)
        if code is not None:
            return code


def _drive(ctx: RunContext) -> int:
    return run_drive(ctx, _default_drive_model())


# --------------------------------------------------------------------------- #
# Bootstrap (--init)
# --------------------------------------------------------------------------- #
def _readme_has_sentinel(path: str) -> bool:
    """True when ``path`` is the harness's shipped template README.

    Detected by the ``README_SENTINEL`` marker the template carries; absence
    means an operator has replaced it with their own project README.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return README_SENTINEL in fh.read()
    except OSError:
        return False


def init(*, from_example: bool = False, force: bool = False) -> int:
    """Prime a fresh project: wipe the template README and seed an AGENTS.md.

    By default the ledger is an empty skeleton (``tasks: {}``) ready for the
    operator's own tasks. ``from_example`` instead stamps the shipped example
    ledger so the bundled demo tasks can be reproduced and self-checked.
    Existing files are preserved unless ``force`` is given.
    """
    try:
        repo = git.Repo(search_parent_directories=True)
        repo_dir = str(repo.working_tree_dir or ".")
    except git.exc.GitError as exc:
        print(f"ERROR: not a usable git repository: {exc}")
        return 1

    print("== harness init ==")

    if from_example:
        try:
            with open(_EXAMPLE_LEDGER_FILE, encoding="utf-8") as fh:
                ledger_body = fh.read()
        except OSError as exc:
            print(f"ERROR: cannot read example ledger {_EXAMPLE_LEDGER_FILE}: {exc}")
            return 1
    else:
        ledger_body = _EMPTY_LEDGER

    agents_path = os.path.join(repo_dir, "AGENTS.md")
    if os.path.exists(agents_path) and not force:
        print("  skip: AGENTS.md already exists (use --force to overwrite).")
    else:
        with open(agents_path, "w", encoding="utf-8") as fh:
            fh.write(ledger_body)
        print(f"  wrote: AGENTS.md ({'example' if from_example else 'empty skeleton'}).")

    readme_path = os.path.join(repo_dir, "README.md")
    if os.path.exists(readme_path) and not _readme_has_sentinel(readme_path) and not force:
        print("  skip: README.md is already project-owned (use --force to overwrite).")
    else:
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write(_PROJECT_README_TEMPLATE)
        print("  wrote: README.md (project stub).")

    print("== init complete ==")
    return 0


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def doctor() -> int:
    """One-pass health report of every coordination subsystem.

    Surfaces the failure modes that are otherwise painful to debug in CI -- a
    stale lease, a corrupt ``contracts.lock``, an unresolved handover, or a
    missing shared ref -- so an operator can diagnose without git archaeology.
    Returns non-zero when a hard problem (corrupt/drifted manifest) is found.
    """
    problems = 0
    print("== harness doctor ==")

    try:
        repo = git.Repo(search_parent_directories=True)
        repo_dir = str(repo.working_tree_dir or ".")
        has_origin = _has_origin(repo)
    except git.exc.GitError as exc:
        print(f"  git: ERROR -- not a usable repository: {exc}")
        return 1
    print(f"  repo: {repo_dir}")
    print(f"  origin remote: {'yes' if has_origin else 'no'}")
    print(f"  minimal mode (shared-ref off): {'yes' if _minimal_mode() else 'no'}")

    print("-- contract manifest --")
    manifest_problems = contract_manifest.verify()
    if manifest_problems:
        problems += 1
        for p in manifest_problems:
            print(f"  PROBLEM: {p}")
    else:
        print("  OK: contracts.lock matches every declared contract.")

    print("-- okf info layer --")
    try:
        okf_problems = okf.verify()
    except (OSError, SystemExit) as exc:
        okf_problems = [f"could not validate OKF info layer: {exc}"]
    if okf_problems:
        problems += 1
        for p in okf_problems:
            print(f"  PROBLEM: {p}")
    else:
        print("  OK: every declared spec_doc is an OKF-conformant concept.")

    print("-- leases --")
    leases_dir = leases.LEASES_DIR
    found = False
    if os.path.isdir(leases_dir):
        for name in sorted(os.listdir(leases_dir)):
            if not name.endswith(".json"):
                continue
            found = True
            task_id = name[: -len(".json")]
            lease = leases.read_lease(task_id)
            if lease is None:
                problems += 1
                print(f"  PROBLEM: {name} is unreadable/corrupt.")
                continue
            state = "ACTIVE" if leases.is_active(lease) else "expired (reclaimable)"
            print(
                f"  {task_id}: {state} -- agent={lease.get('agent_id')} "
                f"branch={lease.get('branch')} created={lease.get('created_at')}"
            )
    if not found:
        print("  (no local leases)")

    print("-- llm seam --")
    if os.getenv("AGENT_ENV_ALLOWLIST"):
        print(
            "  OK: AGENT_ENV_ALLOWLIST set; the LLM subprocess env is scoped "
            "(env_scope=allowlisted)."
        )
    else:
        print(
            "  WARNING: AGENT_ENV_ALLOWLIST not set; the LLM subprocess inherits the "
            "FULL parent environment (env_scope=full_copy). Set it to scope the seam."
        )

    print("-- handover journals (unresolved) --")
    journal_dir = journal.JOURNAL_DIR
    journal_names = (
        [n for n in os.listdir(journal_dir) if n.endswith(".json")]
        if os.path.isdir(journal_dir)
        else []
    )
    unresolved_total = 0
    for name in journal_names:
        try:
            with open(os.path.join(journal_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("outcome") in journal.UNRESOLVED_OUTCOMES:
            unresolved_total += 1
    print(
        f"  journal files: {len(journal_names)} committed "
        f"({unresolved_total} unresolved); these accumulate by design -- see the "
        "README 'Operations' note for the manual cleanup recipe."
    )
    try:
        ledger = _load_ledger()
        task_ids = list((ledger.get("tasks") or {}).keys())
    except SystemExit:
        task_ids = []
    any_unresolved = False
    for task_id in task_ids:
        entry = journal.latest_unresolved(task_id)
        if entry is not None:
            any_unresolved = True
            print(
                f"  {task_id}: unresolved ({entry.get('outcome')}) on "
                f"'{entry.get('branch')}' at {entry.get('finished_at')}"
            )
    if not any_unresolved:
        print("  (none)")

    print("-- shared state ref --")
    if not has_origin:
        print("  (no origin remote; shared-ref coordination inactive)")
    elif _minimal_mode():
        print("  (minimal mode; shared-ref coordination disabled)")
    else:
        files = state_sync.list_files(repo_dir, journal.JOURNAL_DIR)
        if files:
            print(f"  ref '{state_sync.STATE_REF}' carries {len(files)} journal file(s).")
        else:
            print(f"  ref '{state_sync.STATE_REF}' is empty or does not resolve.")

    print("-- project readme --")
    if _readme_has_sentinel(os.path.join(repo_dir, "README.md")):
        print(
            "  WARNING: README.md is still the harness template. Replace it with "
            "your project's README (or run 'python -m harness --init') before starting."
        )
    else:
        print("  OK: root README is project-owned (no template sentinel).")

    print("== doctor complete ==")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Opt-in CLI helpers
# --------------------------------------------------------------------------- #
def _version() -> str:
    """Installed distribution version, falling back to the source constant."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("agent-workflow-harness")
    except PackageNotFoundError:
        return VERSION


def list_tasks() -> int:
    """Enumerate the tasks declared in AGENTS.md (read-only)."""
    tasks = _load_ledger().get("tasks") or {}
    if not tasks:
        print("(no tasks declared in AGENTS.md)")
        return 0
    print("== tasks ==")
    for task_id, raw in tasks.items():
        mode = raw.get("mutation_mode", "?") if isinstance(raw, dict) else "?"
        targets = raw.get("targets") or [] if isinstance(raw, dict) else []
        print(f"  {task_id}: mode={mode}, targets={len(targets)}")
    return 0


def _latest_journal() -> dict[str, Any] | None:
    """Most recent journal entry across all tasks, by ``finished_at``."""
    jdir = journal.JOURNAL_DIR
    if not os.path.isdir(jdir):
        return None
    best: dict[str, Any] | None = None
    best_at = ""
    for name in sorted(os.listdir(jdir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(jdir, name), encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        at = str(entry.get("finished_at", ""))
        if best is None or at > best_at:
            best, best_at = entry, at
    return best


def report_json() -> int:
    """Emit a JSON telemetry/outcome summary of the most recent run."""
    entry = _latest_journal() or {}
    tokens = 0
    cost = 0.0
    try:
        with open(telemetry.usage_file_path(), encoding="utf-8") as fh:
            usage = json.load(fh)
        tokens = int(usage.get("total_tokens", 0) or 0)
        cost = float(usage.get("cost_usd", 0.0) or 0.0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    report = {
        "version": _version(),
        "task_id": entry.get("task_id"),
        "outcome": entry.get("outcome", "none"),
        "branch": entry.get("branch"),
        "finished_at": entry.get("finished_at"),
        "total_tokens": tokens,
        "cost_usd": cost,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def release_lease(task_id: str, assume_yes: bool = False) -> int:
    """Operator escape hatch: force-release a stranded lease for ``task_id``."""
    lease = leases.read_lease(task_id)
    if lease is None:
        print(f"  no local lease recorded for '{task_id}'.")
    else:
        state = "ACTIVE" if leases.is_active(lease) else "expired"
        print(
            f"  local lease ({state}): agent={lease.get('agent_id')} "
            f"branch={lease.get('branch')} created={lease.get('created_at')}"
        )
    if not assume_yes:
        reply = input(f"Force-release the lease for '{task_id}'? [y/N] ").strip().lower()
        if reply not in {"y", "yes"}:
            print("  aborted; no lease was released.")
            return 1

    removed = leases.release(task_id)
    print(f"  local lease {'removed' if removed else 'was already absent'}.")

    try:
        repo = git.Repo(search_parent_directories=True)
        repo_dir = str(repo.working_tree_dir or ".")
        shared = _has_origin(repo) and not _minimal_mode()
    except git.exc.GitError:
        repo_dir, shared = ".", False
    if shared:
        posix_lease = _posix(leases.lease_path(task_id))
        ok = state_sync.publish_files(
            repo_dir,
            {posix_lease: None},
            message=f"harness: force-release lease {task_id}",
        )
        print(f"  shared-ref lease release {'published' if ok else 'FAILED to publish'}.")
    else:
        print("  (no origin / minimal mode): shared-ref release skipped.")
    return 0


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
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print a health report of leases, manifest, journals, and shared ref, then exit.",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Prime a fresh project: replace the template README and seed AGENTS.md, then exit.",
    )
    parser.add_argument(
        "--example",
        action="store_true",
        help="With --init, seed the bundled example ledger instead of an empty skeleton.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --init, overwrite existing AGENTS.md / project README.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the harness version and exit.",
    )
    parser.add_argument(
        "--list",
        dest="list_tasks",
        action="store_true",
        help="List the tasks declared in AGENTS.md and exit.",
    )
    parser.add_argument(
        "--report-json",
        dest="report_json",
        action="store_true",
        help="Print a JSON telemetry/outcome summary of the latest run and exit.",
    )
    parser.add_argument(
        "--release",
        metavar="TASK_ID",
        default=None,
        help="Force-release a stranded lease for TASK_ID and exit.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (used with --release).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"agent-workflow-harness {_version()}")
        return 0
    if args.list_tasks:
        return list_tasks()
    if args.report_json:
        return report_json()
    if args.release:
        return release_lease(args.release, assume_yes=args.yes)
    if args.init:
        return init(from_example=args.example, force=args.force)
    if args.doctor:
        return doctor()
    if not args.task:
        print("ERROR: no task specified (use --task or set AGENT_TASK_ID).")
        return 2

    ctx = initialize(args)
    try:
        isolate(ctx)
        return _drive(ctx)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"unhandled error: {exc}; attempting rollback.")
        ctx.git_warnings.append(f"unhandled error: {exc}")
        report = _build_forensic_report(ctx, "error", f"Unhandled error: {exc}", exit_code=1)
        _persist_journal(ctx, "error", notes=f"Unhandled error: {exc}")
        # After T02 the lease can be held before `checkout -b`, where _rollback
        # early-returns (branch_created is False) and would not release it.
        # _release_lease is idempotent, so call it directly to be safe.
        _release_lease(ctx, commit=False)
        _rollback(ctx)
        _emit_forensic_report(ctx, report)
        return 1


if __name__ == "__main__":
    sys.exit(main())
