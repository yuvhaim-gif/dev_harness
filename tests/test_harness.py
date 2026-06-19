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
HOOK = REPO_ROOT / "harness" / "enforce_file_locks.py"
BINDING_HOOK = REPO_ROOT / "harness" / "enforce_contract_binding.py"
VALIDATOR = REPO_ROOT / "harness" / "validate_agents_ledger.py"
RUNNER = REPO_ROOT / "agent_runner.py"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "harness"))

import contract_manifest  # noqa: E402
import journal  # noqa: E402
import leases  # noqa: E402
import staleness  # noqa: E402
import state_sync  # noqa: E402

from agent_runner import compute_branch_name  # noqa: E402

REFERENCED_PATHS = [
    "example/docs/IMPLEMENTATION.md",
    "example/docs/API_SCHEMA.md",
    "example/tests/test_payments.py",
    "example/tests/test_queries.py",
    "example/src/billing/routes.py",
    "example/src/billing/models.py",
    "example/src/db/queries.py",
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
    _stage(harness_repo, "example/tests/test_queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 1
    assert "outside its allowlist" in res.stdout
    assert "Traceback" not in (res.stdout + res.stderr)


def test_f2b_allows_target_file(harness_repo: Path) -> None:
    _stage(harness_repo, "example/src/db/queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 0, res.stdout + res.stderr


def test_f2c_human_bypass_without_task(harness_repo: Path) -> None:
    _stage(harness_repo, "example/tests/test_queries.py")
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
    _stage(harness_repo, "example/src/db/queries.py")
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


# --------------------------------------------------------------------------- #
# F5. Coordination paths (leases/journal) are always commit-allowed
# --------------------------------------------------------------------------- #
def test_f5_coordination_paths_bypass_allowlist(harness_repo: Path) -> None:
    _write(harness_repo, ".harness/leases/optimise_query_layer.json", "{}\n")
    _git(harness_repo, "add", ".harness/leases/optimise_query_layer.json")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- #
# F6. Contract<->test binding (manifest + bound-test co-touch)
# --------------------------------------------------------------------------- #
def _run_binding(repo: Path, task_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AGENT_TASK_ID"] = task_id
    return subprocess.run(
        [sys.executable, str(BINDING_HOOK)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


def test_f6a_contract_change_without_manifest_is_blocked(harness_repo: Path) -> None:
    _stage(harness_repo, "example/docs/API_SCHEMA.md")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 1
    assert "contracts.lock" in res.stdout


def test_f6b_contract_change_without_bound_test_is_blocked(harness_repo: Path) -> None:
    _stage(harness_repo, "example/docs/API_SCHEMA.md")
    _write(harness_repo, ".harness/contracts.lock", '{"version": 1, "contracts": {}}\n')
    _git(harness_repo, "add", ".harness/contracts.lock")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 1
    assert "contract_tests" in res.stdout


def test_f6c_contract_change_with_manifest_and_test_passes(harness_repo: Path) -> None:
    _stage(harness_repo, "example/docs/API_SCHEMA.md")
    _stage(harness_repo, "example/tests/test_payments.py")
    _write(harness_repo, ".harness/contracts.lock", '{"version": 1, "contracts": {}}\n')
    _git(harness_repo, "add", ".harness/contracts.lock")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 0, res.stdout + res.stderr


def test_f6d_non_contract_change_is_not_gated(harness_repo: Path) -> None:
    _stage(harness_repo, "example/src/billing/routes.py")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- #
# F7. Hashed contract manifest verification
# --------------------------------------------------------------------------- #
def test_f7_manifest_detects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "schema_version: 1\ntasks:\n  t:\n    mutation_mode: evolve\n"
        "    spec_docs: [c.md]\n    contracts: [c.md]\n",
        encoding="utf-8",
    )
    (tmp_path / "c.md").write_text("v1\n", encoding="utf-8")

    contract_manifest.update()
    assert contract_manifest.verify() == []

    (tmp_path / "c.md").write_text("v2 changed\n", encoding="utf-8")
    problems = contract_manifest.verify()
    assert any("drift" in p for p in problems)


# --------------------------------------------------------------------------- #
# F8. Lightweight lease claim
# --------------------------------------------------------------------------- #
def test_f8_lease_blocks_second_agent_then_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ok, _ = leases.acquire("t", "agent/t/1", "agent-a", "base", ["src/x.py"])
    assert ok

    ok2, holder = leases.acquire("t", "agent/t/2", "agent-b", "base", ["src/x.py"])
    assert not ok2
    assert holder is not None
    assert holder["agent_id"] == "agent-a"

    assert leases.release("t")
    ok3, _ = leases.acquire("t", "agent/t/3", "agent-b", "base", ["src/x.py"])
    assert ok3


# --------------------------------------------------------------------------- #
# F9. Handover journal continuity
# --------------------------------------------------------------------------- #
def test_f9_journal_records_unresolved_for_next_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    entry = journal.start_session("t", "agent/t/1", "base")
    journal.record_attempt(entry, "enforce", "semantic", "mypy: incompatible type")
    journal.finalize(entry, "escalated", notes="cap exceeded")
    journal.write(entry)

    recovered = journal.latest_unresolved("t")
    assert recovered is not None
    assert recovered["outcome"] == "escalated"
    assert recovered["attempts"][0]["status"] == "semantic"
    assert journal.latest_unresolved("other_task") is None


# --------------------------------------------------------------------------- #
# F10. Optimistic staleness guard
# --------------------------------------------------------------------------- #
def test_f10_staleness_detects_moved_contract(harness_repo: Path) -> None:
    base = _git(harness_repo, "rev-parse", "HEAD").stdout.strip()
    _git(harness_repo, "checkout", "-b", "other")
    with (harness_repo / "example/docs/API_SCHEMA.md").open("a", encoding="utf-8") as fh:
        fh.write("# moved on shared ref\n")
    _git(harness_repo, "add", "example/docs/API_SCHEMA.md")
    _git(harness_repo, "commit", "-m", "move contract")

    task = {"contracts": ["example/docs/API_SCHEMA.md"]}
    moved = staleness.check(str(harness_repo), base, "other", task)
    assert "example/docs/API_SCHEMA.md" in moved

    unchanged = staleness.check(str(harness_repo), base, base, task)
    assert unchanged == []


# --------------------------------------------------------------------------- #
# F11. Shared-ref state sync survives a fresh clone
# --------------------------------------------------------------------------- #
def test_f11_state_sync_round_trips_across_clones(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))

    clone_a = tmp_path / "a"
    _git(tmp_path, "clone", str(bare), str(clone_a))
    _git(clone_a, "config", "user.email", "a@example.com")
    _git(clone_a, "config", "user.name", "Agent A")
    _write(clone_a, "README.md", "seed\n")
    _git(clone_a, "add", "-A")
    _git(clone_a, "commit", "-m", "seed")
    _git(clone_a, "push", "origin", "main")

    entry = journal.start_session("t", "agent/t/1", "base")
    journal.finalize(entry, "stale", notes="cross-clone handover")
    rel = journal.session_path("agent/t/1").replace("\\", "/")
    (clone_a / rel).parent.mkdir(parents=True, exist_ok=True)
    (clone_a / rel).write_text("placeholder", encoding="utf-8")
    journal.write(entry, journal_dir=str(clone_a / journal.JOURNAL_DIR))

    published = state_sync.publish_files(
        str(clone_a), {rel: rel}, message="harness: journal stale t"
    )
    assert published

    clone_b = tmp_path / "b"
    _git(tmp_path, "clone", str(bare), str(clone_b))
    recovered = state_sync.read_json(str(clone_b), rel)
    assert recovered is not None
    assert recovered["outcome"] == "stale"
    assert rel in state_sync.list_files(str(clone_b), journal.JOURNAL_DIR)
