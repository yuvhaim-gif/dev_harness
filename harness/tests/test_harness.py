"""Verification harness for the agent workflow framework (plan.md Phase F).

Covers:
- F2: lock-hook behaviour (block / allow / human-bypass / corrupt / null-list)
- F3: orchestrator --dry-run smoke test (reaches Reconcile, no commits, manual hint)
- F4: branch-name and ledger-validator guards
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "harness" / "enforce_file_locks.py"
BINDING_HOOK = REPO_ROOT / "harness" / "enforce_contract_binding.py"
VALIDATOR = REPO_ROOT / "harness" / "validate_agents_ledger.py"
RUNNER = REPO_ROOT / "harness" / "agent_runner.py"
CI_ENFORCE = REPO_ROOT / "harness" / "ci_enforce.py"
MANIFEST = REPO_ROOT / "harness" / "contract_manifest.py"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "harness"))

import agent_runner  # noqa: E402
import contract_manifest  # noqa: E402
import git  # noqa: E402
import journal  # noqa: E402
import leases  # noqa: E402
import staleness  # noqa: E402
import state_sync  # noqa: E402
from agent_runner import compute_branch_name  # noqa: E402

REFERENCED_PATHS = [
    "harness/example/docs/IMPLEMENTATION.md",
    "harness/example/docs/API_SCHEMA.md",
    "harness/example/tests/test_payments.py",
    "harness/example/tests/test_queries.py",
    "harness/example/src/billing/routes.py",
    "harness/example/src/billing/models.py",
    "harness/example/src/db/queries.py",
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
    _stage(harness_repo, "harness/example/tests/test_queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 1
    assert "outside its allowlist" in res.stdout
    assert "Traceback" not in (res.stdout + res.stderr)


def test_f2b_allows_target_file(harness_repo: Path) -> None:
    _stage(harness_repo, "harness/example/src/db/queries.py")
    res = _run_hook(harness_repo, {"AGENT_TASK_ID": "optimise_query_layer"})
    assert res.returncode == 0, res.stdout + res.stderr


def test_f2c_human_bypass_without_task(harness_repo: Path) -> None:
    _stage(harness_repo, "harness/example/tests/test_queries.py")
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
    _stage(harness_repo, "harness/example/src/db/queries.py")
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
# F4c. Isolate: lease gates branch creation (T02) + top-level guard (T06)
# --------------------------------------------------------------------------- #
def test_f4c_unique_branch_names_do_not_collide(harness_repo: Path) -> None:
    a = compute_branch_name("t", unique=True)
    b = compute_branch_name("t", unique=True)
    assert a != b
    for name in (a, b):
        res = _git(harness_repo, "check-ref-format", "--branch", name)
        assert res.returncode == 0, res.stdout + res.stderr


def _ctx_for(repo_path: Path, agent_id: str) -> agent_runner.RunContext:
    repo = git.Repo(str(repo_path))
    task = agent_runner._parse_task("optimise_query_layer")
    return agent_runner.RunContext(
        repo=repo,
        task=task,
        dry_run=False,
        agent_id=agent_id,
        base_commit=repo.head.commit.hexsha,
    )


def test_f4c_lease_gates_branch_so_loser_creates_no_branch(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(harness_repo)
    monkeypatch.setenv("AGENT_MINIMAL", "1")  # local-only: no origin needed

    ctx_a = _ctx_for(harness_repo, "agent-a")
    agent_runner.isolate(ctx_a)
    assert ctx_a.branch_created and ctx_a.lease_acquired

    ctx_b = _ctx_for(harness_repo, "agent-b")
    with pytest.raises(SystemExit):
        agent_runner.isolate(ctx_b)
    # The loser took the lease-held path BEFORE creating a branch.
    assert not ctx_b.branch_created
    assert not ctx_b.lease_acquired

    work_branches = [
        b for b in _git(harness_repo, "branch", "--list", "agent/*").stdout.splitlines() if b
    ]
    assert len(work_branches) == 1, work_branches


def test_f4c_release_in_t02_window_clears_lease_without_branch(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the T02 window: the lease is acquired but no branch exists yet.
    # main's guard calls _release_lease(commit=False); it must clear the lease
    # even though branch_created is False (and _rollback would early-return).
    monkeypatch.chdir(harness_repo)
    ctx = _ctx_for(harness_repo, "agent-a")
    ok, _ = leases.acquire(
        task_id=ctx.task.task_id,
        branch="agent/t/x",
        agent_id=ctx.agent_id,
        base_commit=ctx.base_commit,
        targets=ctx.task.targets,
    )
    assert ok
    ctx.lease_acquired = True
    assert not ctx.branch_created

    agent_runner._release_lease(ctx, commit=False)
    assert not ctx.lease_acquired
    assert leases.read_lease(ctx.task.task_id) is None


def test_f4c_isolate_failure_inside_guard_rolls_back_and_releases(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An exception after branch creation must leave the operator on the original
    # branch, with the lease released and a forensic report written (T06).
    monkeypatch.chdir(harness_repo)
    monkeypatch.setenv("AGENT_MINIMAL", "1")
    monkeypatch.setenv("AGENT_ID", "agent-a")

    def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("inject failure after branch creation")

    monkeypatch.setattr(agent_runner.journal, "start_session", boom)

    rc = agent_runner.main(["--task", "optimise_query_layer"])
    assert rc == 1

    branch = _git(harness_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"
    assert leases.read_lease("optimise_query_layer") is None
    assert (harness_repo / ".harness" / "logs" / "FAILED_AGENT_RUN.md").exists()


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
    _stage(harness_repo, "harness/example/docs/API_SCHEMA.md")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 1
    assert "contracts.lock" in res.stdout


def test_f6b_contract_change_without_bound_test_is_blocked(harness_repo: Path) -> None:
    _stage(harness_repo, "harness/example/docs/API_SCHEMA.md")
    _write(harness_repo, ".harness/contracts.lock", '{"version": 1, "contracts": {}}\n')
    _git(harness_repo, "add", ".harness/contracts.lock")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 1
    assert "contract_tests" in res.stdout


def test_f6c_contract_change_with_manifest_and_test_passes(harness_repo: Path) -> None:
    _stage(harness_repo, "harness/example/docs/API_SCHEMA.md")
    _stage(harness_repo, "harness/example/tests/test_payments.py")
    _write(harness_repo, ".harness/contracts.lock", '{"version": 1, "contracts": {}}\n')
    _git(harness_repo, "add", ".harness/contracts.lock")
    res = _run_binding(harness_repo, "add_payments_endpoint")
    assert res.returncode == 0, res.stdout + res.stderr


def test_f6d_non_contract_change_is_not_gated(harness_repo: Path) -> None:
    _stage(harness_repo, "harness/example/src/billing/routes.py")
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


def test_f8b_acquire_writes_atomically_and_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The atomic temp-file + os.replace write (T09) must not change the on-disk
    # byte layout (the shared-ref blob hash depends on it) and must leave no
    # *.tmp file behind in the leases dir.
    monkeypatch.chdir(tmp_path)
    ok, lease = leases.acquire("t", "agent/t/1", "agent-a", "base", ["src/b.py", "src/a.py"])
    assert ok and lease is not None

    raw = Path(leases.lease_path("t")).read_text(encoding="utf-8")
    assert raw == json.dumps(lease, indent=2, sort_keys=True) + "\n"

    leftovers = [p for p in os.listdir(leases.LEASES_DIR) if p.endswith(".tmp")]
    assert leftovers == []


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
    with (harness_repo / "harness/example/docs/API_SCHEMA.md").open("a", encoding="utf-8") as fh:
        fh.write("# moved on shared ref\n")
    _git(harness_repo, "add", "harness/example/docs/API_SCHEMA.md")
    _git(harness_repo, "commit", "-m", "move contract")

    task = {"contracts": ["harness/example/docs/API_SCHEMA.md"]}
    moved = staleness.check(str(harness_repo), base, "other", task)
    assert "harness/example/docs/API_SCHEMA.md" in moved

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


# --------------------------------------------------------------------------- #
# F12. state_sync.publish_files surfaces a hard coordination failure
# --------------------------------------------------------------------------- #
def test_f12_publish_files_returns_false_on_unreachable_remote(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "x@example.com")
    _git(tmp_path, "config", "user.name", "X")
    _write(tmp_path, "f.txt", "hi\n")

    # No 'origin' remote exists, so every push attempt must fail. With
    # backoff_base=0 the retries are instantaneous. The contract is that callers
    # learn about the failure via a False return rather than a silent swallow.
    ok = state_sync.publish_files(
        str(tmp_path),
        {"f.txt": "f.txt"},
        message="m",
        remote="origin",
        attempts=2,
        backoff_base=0,
    )
    assert ok is False


def test_f12b_fetch_ref_logs_failure_not_swallows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal: auth")

    state_sync.reset_fetch_cache()  # a prior memoized success must not short-circuit
    monkeypatch.setattr(state_sync, "_git", fake_git)
    assert state_sync.fetch_ref(".", "harness-state") is False
    out = capsys.readouterr().out
    assert "fetch of 'harness-state'" in out
    assert "fatal: auth" in out


def test_f12c_fetch_ref_memoizes_until_reset_or_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def fake_git(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    state_sync.reset_fetch_cache()
    monkeypatch.setattr(state_sync, "_git", fake_git)

    assert state_sync.fetch_ref(".", "harness-state") is True
    assert state_sync.fetch_ref(".", "harness-state") is True
    assert len(calls) == 1  # the second read is served from the memo

    assert state_sync.fetch_ref(".", "harness-state", refresh=True) is True
    assert len(calls) == 2  # refresh bypasses the memo

    state_sync.reset_fetch_cache()
    assert state_sync.fetch_ref(".", "harness-state") is True
    assert len(calls) == 3  # a reset forces a real fetch again


# --------------------------------------------------------------------------- #
# F13. Corrupt contracts.lock is reported, not crashed on
# --------------------------------------------------------------------------- #
def test_f13_corrupt_lock_reports_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        "AGENTS.md",
        "schema_version: 1\n"
        "tasks:\n"
        "  t:\n"
        "    mutation_mode: evolve\n"
        "    spec_docs: [c.md]\n"
        "    contracts: [c.md]\n",
    )
    _write(tmp_path, "c.md", "v1\n")
    _write(tmp_path, ".harness/contracts.lock", "{ this is not valid json")

    problems = contract_manifest.verify()
    assert any("not valid JSON" in p for p in problems)
    # The actionable remediation command is surfaced rather than a traceback.
    assert any("--update" in p for p in problems)


# --------------------------------------------------------------------------- #
# F14. Server-side CI re-check is authoritative for agent branches
# --------------------------------------------------------------------------- #
def _seed_contract_lock(repo: Path) -> str:
    """Generate + commit a valid manifest on main, returning the base commit."""
    subprocess.run([sys.executable, str(MANIFEST), "--update"], cwd=str(repo), check=True)
    _git(repo, "add", ".harness/contracts.lock")
    _git(repo, "commit", "-m", "lock")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_ci_enforce(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CI_ENFORCE), "--base", base, "--head", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )


def test_f14a_ci_enforce_blocks_out_of_scope_agent_branch(harness_repo: Path) -> None:
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/optimise_query_layer/20260101T000000Z")
    # 'optimise_query_layer' is isolated: only harness/example/src/db/queries.py is allowed.
    with (harness_repo / "harness/example/src/billing/routes.py").open("a", encoding="utf-8") as fh:
        fh.write("# sneaky out-of-scope edit\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "sneaky")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "outside its allowlist" in res.stdout
    assert "harness/example/src/billing/routes.py" in res.stdout


def test_f14b_ci_enforce_allows_in_scope_agent_branch(harness_repo: Path) -> None:
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/optimise_query_layer/20260101T000000Z")
    with (harness_repo / "harness/example/src/db/queries.py").open("a", encoding="utf-8") as fh:
        fh.write("# in-scope optimisation\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "optimise")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 0, res.stdout + res.stderr


def test_f14c_ci_enforce_blocks_contract_change_without_bound_test(harness_repo: Path) -> None:
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/add_payments_endpoint/20260101T000000Z")
    with (harness_repo / "harness/example/docs/API_SCHEMA.md").open("a", encoding="utf-8") as fh:
        fh.write("\n## new field\n")
    subprocess.run([sys.executable, str(MANIFEST), "--update"], cwd=str(harness_repo), check=True)
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "contract change, no test")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "bound" in res.stdout and "contract_test" in res.stdout


def test_f14d_ci_enforce_allows_contract_change_with_bound_test(harness_repo: Path) -> None:
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/add_payments_endpoint/20260101T000000Z")
    schema = harness_repo / "harness/example/docs/API_SCHEMA.md"
    bound_test = harness_repo / "harness/example/tests/test_payments.py"
    with schema.open("a", encoding="utf-8") as fh:
        fh.write("\n## new field\n")
    with bound_test.open("a", encoding="utf-8") as fh:
        fh.write("\n# cover new field\n")
    subprocess.run([sys.executable, str(MANIFEST), "--update"], cwd=str(harness_repo), check=True)
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "contract change with test")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- #
# F15. Bootstrap (--init) and the doctor README sentinel
# --------------------------------------------------------------------------- #
@pytest.fixture()
def empty_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "h@example.com")
    _git(tmp_path, "config", "user.name", "H")
    return tmp_path


def _run_module(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "harness", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )


def test_f15a_init_writes_empty_skeleton_that_validates(empty_repo: Path) -> None:
    res = _run_module(empty_repo, "--init")
    assert res.returncode == 0, res.stdout + res.stderr

    agents = empty_repo / "AGENTS.md"
    readme = empty_repo / "README.md"
    assert agents.exists()
    assert "tasks: {}" in agents.read_text(encoding="utf-8")
    assert readme.exists()
    assert "<!-- HARNESS_TEMPLATE_README" not in readme.read_text(encoding="utf-8")

    validated = subprocess.run(
        [sys.executable, str(VALIDATOR), "AGENTS.md"],
        cwd=str(empty_repo),
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr


def test_f15b_init_example_recreates_shipped_ledger(empty_repo: Path) -> None:
    res = _run_module(empty_repo, "--init", "--example", "--force")
    assert res.returncode == 0, res.stdout + res.stderr

    produced = (empty_repo / "AGENTS.md").read_text(encoding="utf-8")
    shipped = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    example = (REPO_ROOT / "harness" / "example" / "AGENTS.example.md").read_text(encoding="utf-8")
    assert produced == example
    assert produced == shipped, "the example ledger must match the harness's own AGENTS.md"


def test_f15c_doctor_flags_template_readme_then_init_clears_it(empty_repo: Path) -> None:
    (empty_repo / "README.md").write_text(
        "<!-- HARNESS_TEMPLATE_README -->\n# template\n", encoding="utf-8"
    )
    (empty_repo / "AGENTS.md").write_text("schema_version: 1\n\ntasks: {}\n", encoding="utf-8")

    before = _run_module(empty_repo, "--doctor")
    assert before.returncode == 0, before.stdout + before.stderr
    assert "still the harness template" in before.stdout

    init = _run_module(empty_repo, "--init")
    assert init.returncode == 0, init.stdout + init.stderr

    after = _run_module(empty_repo, "--doctor")
    assert after.returncode == 0, after.stdout + after.stderr
    assert "project-owned" in after.stdout


# --------------------------------------------------------------------------- #
# F16. Drive state machine is a testable dispatcher (T13)
# --------------------------------------------------------------------------- #
def _drive_ctx() -> agent_runner.RunContext:
    return agent_runner.RunContext(repo=None, task=None, dry_run=False)  # type: ignore[arg-type]


def _drive_model(**overrides: object) -> agent_runner.DriveModel:
    base: dict[str, object] = {
        "mutate": lambda ctx: None,
        "enforce": lambda ctx: ("passed", ""),
        "autorepair": lambda ctx: True,
        "reconcile": lambda ctx: 0,
        "containment": lambda ctx: False,
        "post_mutate_aborts": (),
        "post_repair_aborts": (),
    }
    base.update(overrides)
    return agent_runner.DriveModel(**base)  # type: ignore[arg-type]


def test_f16a_passed_status_reconciles() -> None:
    model = _drive_model(reconcile=lambda ctx: 0)
    assert agent_runner.run_drive(_drive_ctx(), model) == 0


def test_f16b_dry_run_status_reconciles() -> None:
    model = _drive_model(enforce=lambda ctx: ("dry-run", ""), reconcile=lambda ctx: 7)
    assert agent_runner.run_drive(_drive_ctx(), model) == 7


def test_f16c_mechanical_retries_once_then_passes() -> None:
    calls = {"n": 0}

    def enforce(ctx: object) -> tuple[str, str]:
        calls["n"] += 1
        return ("mechanical", "fixed") if calls["n"] == 1 else ("passed", "")

    model = _drive_model(enforce=enforce, reconcile=lambda ctx: 0)
    assert agent_runner.run_drive(_drive_ctx(), model) == 0
    assert calls["n"] == 2  # the single mechanical retry, then a clean pass


def test_f16d_post_mutate_abort_short_circuits_before_reconcile() -> None:
    reached = {"reconcile": False}

    def reconcile(ctx: object) -> int:
        reached["reconcile"] = True
        return 0

    model = _drive_model(
        post_mutate_aborts=((lambda ctx: True, agent_runner.BUDGET_ABORT_EXIT),),
        reconcile=reconcile,
    )
    assert agent_runner.run_drive(_drive_ctx(), model) == agent_runner.BUDGET_ABORT_EXIT
    assert reached["reconcile"] is False


def test_f16e_post_mutate_guard_abort_exits_four() -> None:
    model = _drive_model(
        post_mutate_aborts=((lambda ctx: True, agent_runner.CONTAINMENT_ABORT_EXIT),)
    )
    assert agent_runner.run_drive(_drive_ctx(), model) == agent_runner.CONTAINMENT_ABORT_EXIT


def test_f16f_autorepair_cap_exits_one() -> None:
    model = _drive_model(enforce=lambda ctx: ("semantic", "boom"), autorepair=lambda ctx: False)
    assert agent_runner.run_drive(_drive_ctx(), model) == 1


def test_f16g_post_repair_abort_breaks_the_loop() -> None:
    model = _drive_model(
        enforce=lambda ctx: ("semantic", "boom"),
        autorepair=lambda ctx: True,
        post_repair_aborts=((lambda ctx: True, agent_runner.BUDGET_ABORT_EXIT),),
    )
    assert agent_runner.run_drive(_drive_ctx(), model) == agent_runner.BUDGET_ABORT_EXIT


def test_f16h_containment_after_pass_exits_four() -> None:
    model = _drive_model(enforce=lambda ctx: ("passed", ""), containment=lambda ctx: True)
    assert agent_runner.run_drive(_drive_ctx(), model) == agent_runner.CONTAINMENT_ABORT_EXIT


# --------------------------------------------------------------------------- #
# F17. Opt-in CLI capabilities (CAP-1..4)
# --------------------------------------------------------------------------- #
def test_f17a_version_flag(harness_repo: Path) -> None:
    res = _run_module(harness_repo, "--version")
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == f"agent-workflow-harness {agent_runner.VERSION}"


def test_f17b_list_enumerates_ledger_tasks(harness_repo: Path) -> None:
    res = _run_module(harness_repo, "--list")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "optimise_query_layer" in res.stdout
    assert "add_payments_endpoint" in res.stdout


def test_f17c_report_json_is_valid_with_expected_keys(harness_repo: Path) -> None:
    res = _run_module(harness_repo, "--report-json")
    assert res.returncode == 0, res.stdout + res.stderr
    data = json.loads(res.stdout)
    assert "total_tokens" in data
    assert "outcome" in data


def test_f17d_release_clears_a_local_lease(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(harness_repo)
    monkeypatch.setenv("AGENT_MINIMAL", "1")  # local-only: no shared ref
    ok, _ = leases.acquire("optimise_query_layer", "agent/x/1", "agent-a", "base", ["x"])
    assert ok
    assert leases.read_lease("optimise_query_layer") is not None

    rc = agent_runner.release_lease("optimise_query_layer", assume_yes=True)
    assert rc == 0
    assert leases.read_lease("optimise_query_layer") is None


# --------------------------------------------------------------------------- #
# F18. Packaging: editable install exposes a console script, script-mode intact
# --------------------------------------------------------------------------- #
def test_f18_editable_install_exposes_console_script(tmp_path: Path) -> None:
    # Install a copy (keeps the repo's working tree clean of build artefacts).
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(REPO_ROOT / "pyproject.toml", proj / "pyproject.toml")
    shutil.copytree(REPO_ROOT / "harness", proj / "harness")

    # A fully isolated venv: no --system-site-packages, so a global editable
    # install of this project cannot shadow the copy under test.
    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    vpy = bin_dir / ("python.exe" if os.name == "nt" else "python")
    script = bin_dir / ("agent-harness.exe" if os.name == "nt" else "agent-harness")

    # Only the import-time runtime deps are needed to probe --version; the rest
    # of the dependency set (e.g. pre-commit) is invoked as subprocesses.
    deps = subprocess.run(
        [str(vpy), "-m", "pip", "install", "gitpython", "pyyaml"],
        capture_output=True,
        text=True,
    )
    assert deps.returncode == 0, deps.stdout + deps.stderr

    inst = subprocess.run(
        [str(vpy), "-m", "pip", "install", "-e", ".", "--no-deps"],
        cwd=str(proj),
        capture_output=True,
        text=True,
    )
    assert inst.returncode == 0, inst.stdout + inst.stderr

    # The console entry point resolves and runs.
    via_script = subprocess.run([str(script), "--version"], capture_output=True, text=True)
    assert via_script.returncode == 0, via_script.stdout + via_script.stderr
    assert "agent-workflow-harness" in via_script.stdout

    # The pre-commit-style script invocation still works (flat imports intact).
    via_module = subprocess.run(
        [str(vpy), str(proj / "harness" / "agent_runner.py"), "--version"],
        capture_output=True,
        text=True,
    )
    assert via_module.returncode == 0, via_module.stdout + via_module.stderr
    assert "agent-workflow-harness" in via_module.stdout
