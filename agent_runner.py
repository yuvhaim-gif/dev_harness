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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import git
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "hooks"))

from lock_policy import compute_allowlist  # noqa: E402

SUPPORTED_SCHEMA_VERSION = 1


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
    raw: dict[str, Any]


@dataclass
class RunContext:
    repo: git.Repo
    task: TaskSpec
    dry_run: bool
    original_branch: str = ""
    work_branch: str = ""
    autorepair_attempts: int = 0
    last_hook_log: str = ""
    branch_created: bool = field(default=False)


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
        raw=raw,
    )


def _has_origin(repo: git.Repo) -> bool:
    return any(remote.name == "origin" for remote in repo.remotes)


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
    log(f"initialized for task '{task.task_id}' (mode={task.mutation_mode}).")
    return RunContext(repo=repo, task=task, dry_run=bool(args.dry_run))


# --------------------------------------------------------------------------- #
# E3. State 2 — Isolate
# --------------------------------------------------------------------------- #
def compute_branch_name(task_id: str, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    # NOTE: strftime form is colon-free; isoformat() emits ':' and '+',
    # which git check-ref-format rejects.
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"agent/{task_id}/{stamp}"


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
def _rollback(ctx: RunContext) -> None:
    if ctx.dry_run or not ctx.branch_created or not ctx.original_branch:
        return
    try:
        ctx.repo.git.checkout(ctx.original_branch)
        log(f"rolled back to original branch '{ctx.original_branch}'.")
    except git.exc.GitCommandError as exc:
        log(f"WARNING: rollback checkout failed: {exc}")


def autorepair(ctx: RunContext) -> bool:
    """Return True to retry the loop, False to escalate (caller should stop)."""
    ctx.autorepair_attempts += 1
    if ctx.autorepair_attempts > ctx.task.max_autorepair_attempts:
        log(
            "escalating: exceeded max_autorepair_attempts "
            f"({ctx.task.max_autorepair_attempts}); rolling back."
        )
        _rollback(ctx)
        return False
    log(
        f"autorepair attempt {ctx.autorepair_attempts}/"
        f"{ctx.task.max_autorepair_attempts}: feeding hook log to fix loop (LLM seam)."
    )
    return True


# --------------------------------------------------------------------------- #
# E7. State 5B — Reconcile
# --------------------------------------------------------------------------- #
def reconcile(ctx: RunContext) -> None:
    branch = ctx.work_branch
    has_origin = _has_origin(ctx.repo)

    if ctx.dry_run:
        log(f"[dry-run] would run: git push -u origin {branch}")
        if has_origin:
            log("[dry-run] would open a PR via 'gh pr create' (if gh is available).")
        else:
            log(f"[dry-run] no 'origin' remote; manual push: git push -u origin {branch}")
        return

    if not has_origin:
        log(f"No 'origin' remote configured. Manual push: git push -u origin {branch}")
        return

    ctx.repo.git.push("-u", "origin", branch)
    log(f"pushed branch '{branch}' to origin.")

    if shutil.which("gh"):
        labels = ",".join(ctx.task.pr_labels)
        cmd = ["gh", "pr", "create", "--fill"]
        if labels:
            cmd += ["--label", labels]
        subprocess.run(cmd, check=False)
        log("requested PR creation via 'gh'.")
    else:
        labels = ",".join(ctx.task.pr_labels)
        hint = "gh pr create --fill" + (f" --label {labels}" if labels else "")
        log(f"GitHub CLI 'gh' not found. Manual PR: {hint}")


# --------------------------------------------------------------------------- #
# E8. CLI / main loop
# --------------------------------------------------------------------------- #
def _drive(ctx: RunContext) -> int:
    while True:
        mutate(ctx)
        status, log_text = enforce(ctx)

        if status == "dry-run":
            reconcile(ctx)
            return 0

        if status == "mechanical":
            log("mechanical hook fix detected; re-staging and retrying once.")
            status, log_text = enforce(ctx)

        if status == "passed":
            reconcile(ctx)
            return 0

        # semantic (or still mechanical after the single retry) -> autorepair
        ctx.last_hook_log = log_text
        if not autorepair(ctx):
            return 1


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
        _rollback(ctx)
        return 1


if __name__ == "__main__":
    sys.exit(main())
