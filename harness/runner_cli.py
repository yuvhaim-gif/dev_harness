"""CLI surface: init, doctor, reporting, lease release, and main()."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from typing import Any

import contract_manifest
import git
import journal
import leases
import okf
import state_sync
import telemetry
from runner_core import (
    _EMPTY_LEDGER,
    _EXAMPLE_LEDGER_FILE,
    _PROJECT_README_TEMPLATE,
    README_SENTINEL,
    VERSION,
    _has_origin,
    _load_ledger,
    _minimal_mode,
    _posix,
    log,
)
from runner_drive import _drive
from runner_recovery import (
    _build_forensic_report,
    _emit_forensic_report,
    _persist_journal,
    _release_lease,
    _rollback,
)
from runner_states import initialize, isolate


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
            "FULL parent environment (env_scope=full_copy). Set it to scope the seam. "
            "Set AGENT_ENV_STRICT=1 to refuse a full-copy run."
        )

    print("-- handover journals (unresolved) --")
    journal_dir = journal.JOURNAL_DIR
    journal_names = (
        [n for n in os.listdir(journal_dir) if n.endswith(".json")]
        if os.path.isdir(journal_dir)
        else []
    )
    # One pass over the journal dir feeds both the count and the per-task
    # latest-unresolved lookup, instead of re-scanning once per declared task.
    unresolved_total = 0
    latest_by_task: dict[str, dict[str, Any]] = {}
    for name in journal_names:
        try:
            with open(os.path.join(journal_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if not (isinstance(data, dict) and data.get("outcome") in journal.UNRESOLVED_OUTCOMES):
            continue
        unresolved_total += 1
        tid = str(data.get("task_id", ""))
        current = latest_by_task.get(tid)
        finished_at = str(data.get("finished_at", ""))
        if current is None or finished_at >= str(current.get("finished_at", "")):
            latest_by_task[tid] = data
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
        entry = latest_by_task.get(str(task_id))
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
    if not leases.is_valid_task_id(task_id):
        print(f"ERROR: refusing to release unsafe task id '{task_id}'.")
        return 2
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
