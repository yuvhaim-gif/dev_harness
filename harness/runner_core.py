"""Core data models, constants, ledger parsing, and shared repo helpers."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

import git
import leases
import ledger as ledger_io
import telemetry
from lock_policy import env_flag

SUPPORTED_SCHEMA_VERSION = 1


SHARED_REF = os.getenv("AGENT_SHARED_REF", "origin/main")


REPAIR_PROMPT_FILE = ".harness/telemetry/repair_prompt.txt"


BUDGET_ABORT_EXIT = 3


CONTAINMENT_ABORT_EXIT = 4


class ContainmentCheckError(RuntimeError):
    """A committed-state containment probe could not run (git error).

    Raised by the ``base..HEAD`` probes when git itself fails (e.g. the base
    commit's objects were deleted). It is caught in ``_containment_breach`` and
    turned into an explicit violation so the gate fails *closed* -- an
    un-runnable containment check is a breach, never a clean bill of health.
    """


VERSION = "0.1.0"


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
    return env_flag("AGENT_MINIMAL") or env_flag("AGENT_DISABLE_STATE_SYNC")


def log(msg: str) -> None:
    print(f"[agent_runner] {msg}")


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


def _load_ledger(path: str = "AGENTS.md") -> dict[str, Any]:
    try:
        data = ledger_io.load_ledger(path)
    except ledger_io.LedgerError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    schema_version = data.get("schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != SUPPORTED_SCHEMA_VERSION
    ):
        raise SystemExit(
            f"ERROR: unsupported schema_version {schema_version!r} "
            f"(this runner supports {SUPPORTED_SCHEMA_VERSION})."
        )
    return data


def _parse_task(task_id: str) -> TaskSpec:
    ledger = _load_ledger()
    if not leases.is_valid_task_id(task_id):
        raise SystemExit(
            f"ERROR: task id '{task_id}' is not a safe slug ([A-Za-z0-9][A-Za-z0-9._-]*, no '..')."
        )
    raw = ledger_io.get_task(ledger, task_id)
    if raw is None:
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
