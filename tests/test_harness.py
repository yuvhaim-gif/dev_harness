"""Verification harness for the agent workflow framework (plan.md Phase F).

Covers:
- F2: lock-hook behaviour (block / allow / human-bypass / corrupt / null-list)
- F3: orchestrator --dry-run smoke test (reaches Reconcile, no commits, manual hint)
- F4: branch-name and ledger-validator guards
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "hooks" / "enforce_file_locks.py"
VALIDATOR = REPO_ROOT / "scripts" / "hooks" / "validate_agents_ledger.py"
RUNNER = REPO_ROOT / "agent_runner.py"

sys.path.insert(0, str(REPO_ROOT))

from agent_runner import compute_branch_name  # noqa: E402

REFERENCED_PATHS = [
    "docs/IMPLEMENTATION.md",
    "docs/API_SCHEMA.md",
    "tests/test_payments.py",
    "tests/test_queries.py",
    "src/billing/routes.py",
    "src/billing/models.py",
    "src/db/queries.py",
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def _write(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _run_hook(repo: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def harness_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "harness@example.com")
    _git(repo, "config", "user.name", "Harness")

    ledger = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    _write(repo, "AGENTS.md", ledger)
    for rel in REFERENCED_PATHS:
        _write(repo, rel, f"# placeholder for {rel}\n")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _stage(repo: Path, rel: str) -> None:
    with (repo / rel).open("a", encoding="utf-8") as fh:
        fh.write("# touched\n")
    _git(repo, "add", rel)


# --------------------------------------------------------------------------- #
# F2. Lock-hook behaviour
# --------------------------------------------------------------------------- #
def test_f2a_blocks_locked_file_in_isolated_mode(harness_repo: Path) -> None:
    _stage(harness_repo, "tests/test_queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 1
    assert "outside its allowlist" in res.stdout
    assert "Traceback" not in (res.stdout + res.stderr)


def test_f2b_allows_target_file(harness_repo: Path) -> None:
    _stage(harness_repo, "src/db/queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 0, res.stdout + res.stderr


def test_f2c_human_bypass_without_task(harness_repo: Path) -> None:
    _stage(harness_repo, "tests/test_queries.py")
    env = os.environ.copy()
    env.pop("AGENT_TASK_ID", None)
    res = subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_f2d_corrupt_ledger_aborts_cleanly(harness_repo: Path) -> None:
    _write(harness_repo, "AGENTS.md", "tasks: [unclosed\n  : : :\n")
    _stage(harness_repo, "src/db/queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 1
    assert "not valid YAML" in res.stdout
    assert "Traceback" not in (res.stdout + res.stderr)


def test_f2e_null_target_list_does_not_crash(harness_repo: Path) -> None:
    ledger = "schema_version: 1\ntasks:\n  null_task:\n    mutation_mode: isolated\n    targets:\n"
    _write(harness_repo, "AGENTS.md", ledger)
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "null_task"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Traceback" not in (res.stdout + res.stderr)


# --------------------------------------------------------------------------- #
# F3. Dry-run smoke test
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("task", ["add_payments_endpoint", "optimise_query_layer"])
def test_f3_dry_run_reaches_reconcile_without_commits(harness_repo: Path, task: str) -> None:
    before = _git(harness_repo, "rev-list", "--count", "HEAD").stdout.strip()
    res = subprocess.run(
        [sys.executable, str(RUNNER), "--task", task, "--dry-run"],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
    )
    after = _git(harness_repo, "rev-list", "--count", "HEAD").stdout.strip()
    combined = res.stdout + res.stderr
    assert res.returncode == 0, combined
    assert before == after, "dry-run must not create commits"
    assert "git push -u origin" in combined
    branches = _git(harness_repo, "branch", "--list").stdout
    assert "agent/" not in branches, "dry-run must not create a work branch"


# --------------------------------------------------------------------------- #
# F4. Branch-name & ledger guards
# --------------------------------------------------------------------------- #
def test_f4a_computed_branch_name_is_valid(harness_repo: Path) -> None:
    name = compute_branch_name("optimise_query_layer")
    res = _git(harness_repo, "check-ref-format", "--branch", name)
    assert res.returncode == 0, res.stdout + res.stderr


def test_f4a_isoformat_branch_name_is_rejected(harness_repo: Path) -> None:
    bad = f"agent/x/{datetime.now(UTC).isoformat()}"
    res = _git(harness_repo, "check-ref-format", "--branch", bad)
    assert res.returncode != 0


def test_f4b_validator_passes_on_shipped_ledger(harness_repo: Path) -> None:
    res = subprocess.run(
        [sys.executable, str(VALIDATOR), "AGENTS.md"],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_f4b_validator_fails_on_corrupt_ledger(harness_repo: Path) -> None:
    _write(harness_repo, "AGENTS.md", "tasks: [unclosed\n")
    res = subprocess.run(
        [sys.executable, str(VALIDATOR), "AGENTS.md"],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
