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
import threading
import time
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
import runner_reconcile  # noqa: E402
import runner_recovery  # noqa: E402
import staleness  # noqa: E402
import state_sync  # noqa: E402
from agent_runner import compute_branch_name  # noqa: E402

REFERENCED_PATHS = [
    "harness/example/docs/IMPLEMENTATION.md",
    "harness/example/docs/API_SCHEMA.md",
    "harness/example/docs/index.md",
    "harness/example/docs/log.md",
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
        # Spec_docs are OKF concept files the info-layer gate validates; copy the
        # real (frontmatter-bearing) bundle so the fixture is OKF-conformant.
        src = REPO_ROOT / rel
        if rel.endswith(".md") and src.exists():
            _write(repo, rel, src.read_text(encoding="utf-8"))
        else:
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


def test_f4d_validator_rejects_non_int_attempts_and_scalar_list_field(
    harness_repo: Path,
) -> None:
    ledger = (
        "schema_version: 1\n"
        "tasks:\n"
        "  bad:\n"
        "    mutation_mode: isolated\n"
        "    max_autorepair_attempts: three\n"
        "    targets: not-a-list\n"
    )
    _write(harness_repo, "AGENTS.md", ledger)
    res = subprocess.run(
        [sys.executable, str(VALIDATOR), "AGENTS.md"],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1
    assert "max_autorepair_attempts must be an integer" in res.stdout
    assert "field 'targets' must be a list" in res.stdout


def test_f4d_parse_task_rejects_bad_attempts(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = (
        "schema_version: 1\n"
        "tasks:\n"
        "  bad:\n"
        "    mutation_mode: isolated\n"
        "    max_autorepair_attempts: three\n"
    )
    _write(harness_repo, "AGENTS.md", ledger)
    monkeypatch.chdir(harness_repo)
    with pytest.raises(SystemExit):
        agent_runner._parse_task("bad")


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


def test_acquire_exclusive_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A fresh claim takes the O_EXCL create fast path and stays byte-stable.
    monkeypatch.chdir(tmp_path)
    ok, lease = leases.acquire("t", "agent/t/1", "agent-a", "base", ["src/b.py", "src/a.py"])
    assert ok and lease is not None

    raw = Path(leases.lease_path("t")).read_text(encoding="utf-8")
    assert raw == json.dumps(lease, indent=2, sort_keys=True) + "\n"
    assert [p for p in os.listdir(leases.LEASES_DIR) if p.endswith(".tmp")] == []


def test_acquire_blocks_live_other_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    ok, _ = leases.acquire("t", "agent/t/1", "agent-a", "base", ["x"])
    assert ok

    ok2, holder = leases.acquire("t", "agent/t/2", "agent-b", "base", ["x"])
    assert not ok2
    assert holder is not None and holder["agent_id"] == "agent-a"


def test_acquire_reclaims_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An expired lease is present, so the create fast path is skipped and the
    # atomic-replace reclaim takes over, handing ownership to the new agent.
    monkeypatch.chdir(tmp_path)
    expired = {
        "task_id": "t",
        "branch": "agent/t/old",
        "agent_id": "agent-b",
        "base_commit": "base",
        "targets": [],
        "created_at": "2000-01-01T00:00:00Z",
        "ttl_seconds": 1,
    }
    os.makedirs(leases.LEASES_DIR, exist_ok=True)
    Path(leases.lease_path("t")).write_text(
        json.dumps(expired, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    ok, lease = leases.acquire("t", "agent/t/new", "agent-a", "base", ["x"])
    assert ok and lease is not None and lease["agent_id"] == "agent-a"
    on_disk = leases.read_lease("t")
    assert on_disk is not None and on_disk["agent_id"] == "agent-a"
    assert [p for p in os.listdir(leases.LEASES_DIR) if p.endswith(".tmp")] == []


def test_acquire_lost_race_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # We read "absent", enter the exclusive-create fast path, but lose the race:
    # the create fails and a live OTHER-agent lease is now present, so we back off.
    monkeypatch.chdir(tmp_path)
    competitor = {
        "task_id": "t",
        "branch": "agent/t/b",
        "agent_id": "agent-b",
        "base_commit": "base",
        "targets": [],
        "created_at": leases._stamp(leases._now()),
        "ttl_seconds": leases.DEFAULT_TTL_SECONDS,
    }
    reads = iter([None, competitor])  # 1st: absent; 2nd (post-race): live competitor
    monkeypatch.setattr(leases, "read_lease", lambda *a, **k: next(reads))

    def boom(*_a: object, **_k: object) -> int:
        raise FileExistsError

    monkeypatch.setattr(leases.os, "open", boom)

    ok, holder = leases.acquire("t", "agent/t/a", "agent-a", "base", ["x"])
    assert not ok
    assert holder is not None and holder["agent_id"] == "agent-b"


def test_is_active_nonnumeric_ttl_is_inactive() -> None:
    # A corrupted/adversarial lease with a non-numeric ttl must not crash the
    # check the whole coordination layer depends on; it is treated as inactive
    # (reclaimable) rather than raising an uncaught ValueError.
    lease = {"created_at": leases._stamp(leases._now()), "ttl_seconds": "not-a-number"}
    assert leases.is_active(lease) is False
    lease2 = {"created_at": leases._stamp(leases._now()), "ttl_seconds": None}
    assert leases.is_active(lease2) is False


def _seed_expired_lease(leases_dir: str, agent_id: str = "agent-old") -> None:
    expired = {
        "task_id": "t",
        "branch": "agent/t/old",
        "agent_id": agent_id,
        "base_commit": "base",
        "targets": [],
        "created_at": "2000-01-01T00:00:00Z",
        "ttl_seconds": 1,
    }
    os.makedirs(leases_dir, exist_ok=True)
    Path(leases.lease_path("t", leases_dir)).write_text(
        json.dumps(expired, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_acquire_single_winner_under_concurrent_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The TOCTOU race: with no reclaim mutex, many agents that all read the same
    # expired lease each os.replace their own copy and ALL return success. The
    # os.mkdir mutex must serialise the reclaim so exactly one agent wins.
    monkeypatch.chdir(tmp_path)
    _seed_expired_lease(leases.LEASES_DIR)

    n = 30
    barrier = threading.Barrier(n)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()  # release all racers simultaneously
        ok, _ = leases.acquire("t", f"agent/t/{i}", f"agent-{i}", "base", ["x"])
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1, f"expected exactly one winner, got {results.count(True)}"
    on_disk = leases.read_lease("t")
    assert on_disk is not None and on_disk["agent_id"].startswith("agent-")
    # No reclaim mutex dir and no temp files left behind.
    leftovers = os.listdir(leases.LEASES_DIR)
    assert not any(p.endswith((".tmp", ".lock")) for p in leftovers), leftovers


def test_reclaim_mutex_stale_takeover_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stale-lock takeover must not use rmdir+mkdir (two syscalls) which lets
    # a second racer clobber the winner's fresh lock and both believe they hold
    # the mutex. A fresh lock is never stealable; a genuinely stale one is stolen
    # by exactly one racer.
    lock_dir = str(tmp_path / "t.json.lock")

    # Fresh lock: not stale -> cannot be taken over.
    os.mkdir(lock_dir)
    assert leases._acquire_reclaim_mutex(lock_dir) is False

    # Make it look stale by backdating its mtime past the staleness window.
    old = time.time() - (leases._RECLAIM_LOCK_STALE_SECONDS + 10)
    os.utime(lock_dir, (old, old))
    assert leases._acquire_reclaim_mutex(lock_dir) is True  # exactly one steal
    assert os.path.isdir(lock_dir)
    # The transient steal directory is cleaned up.
    assert not any(".stealing-" in p for p in os.listdir(tmp_path))


def test_reclaim_mutex_restores_lock_recreated_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate the compound race: our first mtime read saw the lock as stale, but
    # a racer recreated it (fresh) before our rename. The second age check must
    # detect the fresh lock, restore it, and back off rather than double-claim.
    lock_dir = str(tmp_path / "t.json.lock")
    os.mkdir(lock_dir)
    old = time.time() - (leases._RECLAIM_LOCK_STALE_SECONDS + 10)
    os.utime(lock_dir, (old, old))

    real_getmtime = leases.os.path.getmtime
    calls = {"n": 0}

    def flaky_getmtime(path: str) -> float:
        calls["n"] += 1
        # 1st call (pre-rename age check): report stale.
        # 2nd call (post-rename freshness check): report fresh.
        return old if calls["n"] == 1 else time.time()

    monkeypatch.setattr(leases.os.path, "getmtime", flaky_getmtime)

    assert leases._acquire_reclaim_mutex(lock_dir) is False
    monkeypatch.setattr(leases.os.path, "getmtime", real_getmtime)
    # The lock was restored, not stolen; no transient steal dir left behind.
    assert os.path.isdir(lock_dir)
    assert not any(".stealing-" in p for p in os.listdir(tmp_path))


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


def test_f10b_staleness_includes_task_targets(harness_repo: Path) -> None:
    # critical_paths() must include the task's own targets, so that if two agents
    # race the lease for an isolated-mode task, the loser is still caught when a
    # target it built on has moved on the shared ref. Before the fix targets were
    # omitted and a moved target slipped past the staleness guard entirely.
    target = "harness/example/src/db/queries.py"
    assert target in staleness.critical_paths({"targets": [target]})

    base = _git(harness_repo, "rev-parse", "HEAD").stdout.strip()
    _git(harness_repo, "checkout", "-b", "other")
    with (harness_repo / target).open("a", encoding="utf-8") as fh:
        fh.write("# target moved on shared ref\n")
    _git(harness_repo, "add", target)
    _git(harness_repo, "commit", "-m", "move target")

    moved = staleness.check(str(harness_repo), base, "other", {"targets": [target]})
    assert target in moved


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
    _write(tmp_path, ".harness/leases/f.json", "hi\n")

    # No 'origin' remote exists, so every push attempt must fail. With
    # backoff_base=0 the retries are instantaneous. The contract is that callers
    # learn about the failure via a False return rather than a silent swallow.
    ok = state_sync.publish_files(
        str(tmp_path),
        {".harness/leases/f.json": ".harness/leases/f.json"},
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


def test_f14e_ci_enforce_blocks_smuggled_py_under_journal(harness_repo: Path) -> None:
    # The most serious finding: a branch pushed *directly* (never touching the
    # local orchestrator or its SHA-based out-of-band backstop) smuggles an
    # arbitrary .py under the allowlist-exempt .harness/journal/. ci_enforce is
    # the only gate on this path and must reject the malformed coordination blob.
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/optimise_query_layer/20260101T000000Z")
    _write(harness_repo, ".harness/journal/payload.py", "import os  # injected payload\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "smuggle payload via journal")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "invalid coordination payload" in res.stdout
    assert ".harness/journal/payload.py" in res.stdout


def test_f14f_ci_enforce_blocks_unknown_shaped_journal_json(harness_repo: Path) -> None:
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/optimise_query_layer/20260101T000000Z")
    _write(harness_repo, ".harness/journal/x.json", json.dumps({"evil": "payload"}) + "\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "unknown-shaped journal json")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 1, res.stdout + res.stderr
    assert "invalid coordination payload" in res.stdout


def test_f14g_ci_enforce_allows_valid_journal_json(harness_repo: Path) -> None:
    # A well-formed journal artifact is the legitimate exempt case and must pass.
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "agent/optimise_query_layer/20260101T000000Z")
    payload = {"task_id": "optimise_query_layer", "outcome": "escalated", "attempts": []}
    _write(harness_repo, ".harness/journal/optimise_query_layer.json", json.dumps(payload) + "\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "valid journal entry")

    res = _run_ci_enforce(harness_repo, base)
    assert res.returncode == 0, res.stdout + res.stderr


def test_f14h_ci_enforce_fails_closed_on_non_agent_branch(harness_repo: Path) -> None:
    # F1 regression: an agent could dodge the file-scope re-check by pushing its
    # work on a branch that does not match agent/<task>/... . With no trusted
    # task id supplied that must now FAIL closed rather than SKIP-and-pass.
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "sneaky")
    with (harness_repo / "harness/example/src/billing/routes.py").open("a", encoding="utf-8") as fh:
        fh.write("# out-of-scope edit on a non-agent branch\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "sneaky")

    env = os.environ.copy()
    env.pop("HARNESS_NON_AGENT_OK", None)
    env.pop("AGENT_TASK_ID", None)
    res = subprocess.run(
        [sys.executable, str(CI_ENFORCE), "--base", base, "--head", "HEAD"],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "cannot determine task" in res.stdout


def test_f14i_ci_enforce_trusts_injected_task_over_branch_name(harness_repo: Path) -> None:
    # A CI-injected task id is authoritative regardless of the branch name, so an
    # out-of-scope change is still caught on an arbitrarily named branch.
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "sneaky")
    with (harness_repo / "harness/example/src/billing/routes.py").open("a", encoding="utf-8") as fh:
        fh.write("# out-of-scope edit\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "sneaky")

    res = subprocess.run(
        [
            sys.executable,
            str(CI_ENFORCE),
            "--base",
            base,
            "--head",
            "HEAD",
            "--task",
            "optimise_query_layer",
        ],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1, res.stdout + res.stderr
    assert "outside its allowlist" in res.stdout


def test_f14j_ci_enforce_allows_declared_human_branch(harness_repo: Path) -> None:
    # A genuine human PR opts out of file-scope via the trusted, workflow-set
    # HARNESS_NON_AGENT_OK flag (the agent cannot set it); the manifest check
    # still runs, but the branch is not held to a task allowlist.
    base = _seed_contract_lock(harness_repo)
    _git(harness_repo, "checkout", "-b", "feature/manual")
    with (harness_repo / "harness/example/src/billing/routes.py").open("a", encoding="utf-8") as fh:
        fh.write("# human change\n")
    _git(harness_repo, "add", "-A")
    _git(harness_repo, "commit", "-m", "human work")

    env = os.environ.copy()
    env["HARNESS_NON_AGENT_OK"] = "1"
    res = subprocess.run(
        [sys.executable, str(CI_ENFORCE), "--base", base, "--head", "HEAD"],
        cwd=str(harness_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "human-authored" in res.stdout


def test_f14k_resolve_base_falls_back_to_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    # F6 regression: GITHUB_BASE_REF is a bare name (e.g. `main`) that may not
    # resolve in a shallow/detached checkout; _resolve_base must fall back to
    # origin/<name> so the diff range is never silently empty.
    import ci_enforce

    def fake_git(*args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("rev-parse", "--verify"):
            ok = args[-1].startswith("origin/main")
            return subprocess.CompletedProcess(list(args), 0 if ok else 1, "", "")
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(ci_enforce, "_git", fake_git)
    assert ci_enforce._resolve_base("main") == "origin/main"
    assert ci_enforce._resolve_base("origin/main") == "origin/main"


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


def test_f15d_doctor_reports_journal_file_count(empty_repo: Path) -> None:
    (empty_repo / "AGENTS.md").write_text("schema_version: 1\n\ntasks: {}\n", encoding="utf-8")
    jdir = empty_repo / ".harness" / "journal"
    jdir.mkdir(parents=True)
    (jdir / "agent__t__1.json").write_text(
        json.dumps({"task_id": "t", "branch": "agent/t/1", "outcome": "error"}),
        encoding="utf-8",
    )
    (jdir / "agent__t__2.json").write_text(
        json.dumps({"task_id": "t", "branch": "agent/t/2", "outcome": "pushed"}),
        encoding="utf-8",
    )

    res = _run_module(empty_repo, "--doctor")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "journal files: 2 committed (1 unresolved)" in res.stdout


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


# --------------------------------------------------------------------------- #
# F19. Reconcile push-failure recovery (recoverable error outcome)
# --------------------------------------------------------------------------- #
class _RecordingGit:
    """Wrap a real ``git.Git`` but intercept ``push`` with a test double."""

    def __init__(self, real: object, push: object) -> None:
        self._real = real
        self._push = push

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    def push(self, *args: object, **kwargs: object) -> object:
        return self._push(*args, **kwargs)  # type: ignore[operator]


def _reconcile_ctx(repo_path: Path) -> agent_runner.RunContext:
    ctx = _ctx_for(repo_path, "agent-a")
    ctx.work_branch = "agent/optimise_query_layer/recon"
    ctx.journal_entry = journal.start_session(ctx.task.task_id, ctx.work_branch, ctx.base_commit)
    return ctx


def _journal_outcome(branch: str) -> str:
    data = json.loads(Path(journal.session_path(branch)).read_text(encoding="utf-8"))
    return str(data["outcome"])


def test_reconcile_success_pushes_and_journals_pushed(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(harness_repo)
    monkeypatch.setenv("AGENT_MINIMAL", "1")  # local-only: skip shared-ref publish
    monkeypatch.setattr(runner_reconcile, "_has_origin", lambda repo: True)
    monkeypatch.setattr(runner_reconcile, "_staleness_guard", lambda ctx: [])
    monkeypatch.setattr(runner_reconcile, "_open_pr", lambda ctx: None)

    ctx = _reconcile_ctx(harness_repo)
    calls = {"n": 0}

    def push(*args: object, **kwargs: object) -> str:
        calls["n"] += 1
        return ""

    monkeypatch.setattr(ctx.repo, "git", _RecordingGit(ctx.repo.git, push))

    assert agent_runner.reconcile(ctx) == 0
    assert calls["n"] == 1
    assert _journal_outcome(ctx.work_branch) == "pushed"


def test_reconcile_failed_push_marks_error_and_returns_1(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(harness_repo)
    monkeypatch.setenv("AGENT_MINIMAL", "1")
    monkeypatch.setattr(runner_reconcile, "_has_origin", lambda repo: True)
    monkeypatch.setattr(runner_reconcile, "_staleness_guard", lambda ctx: [])
    monkeypatch.setattr(runner_reconcile, "_open_pr", lambda ctx: None)

    ctx = _reconcile_ctx(harness_repo)
    calls = {"n": 0}

    def push(*args: object, **kwargs: object) -> str:
        calls["n"] += 1
        raise git.exc.GitCommandError(["push"], 128, b"remote rejected")

    monkeypatch.setattr(ctx.repo, "git", _RecordingGit(ctx.repo.git, push))

    assert agent_runner.reconcile(ctx) == 1
    assert calls["n"] == 1
    # The terminal state is the recoverable 'error', not the optimistic 'pushed'.
    assert _journal_outcome(ctx.work_branch) == "error"
    assert "error" in journal.UNRESOLVED_OUTCOMES


# --------------------------------------------------------------------------- #
# F20. The runner's own commit env never inherits the human override
# --------------------------------------------------------------------------- #
def _minimal_task(task_id: str = "t") -> agent_runner.TaskSpec:
    return agent_runner.TaskSpec(
        task_id=task_id,
        description="",
        mutation_mode="isolated",
        spec_docs=[],
        tests=[],
        targets=[],
        locked_files=[],
        commit_prefix="chore",
        max_autorepair_attempts=3,
        pr_labels=[],
        contracts=[],
        contract_tests=[],
        raw={},
    )


def test_commit_env_drops_human_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKIP_AGENT_HARNESS", "1")
    ctx = agent_runner.RunContext(
        repo=None,  # type: ignore[arg-type]
        task=_minimal_task("payments"),
        dry_run=False,
    )
    env = agent_runner._commit_env(ctx)
    assert "SKIP_AGENT_HARNESS" not in env
    assert env["AGENT_TASK_ID"] == "payments"


# --------------------------------------------------------------------------- #
# F21. Autorepair journals the real enforce status, not a hardcoded label
# --------------------------------------------------------------------------- #
def test_autorepair_records_real_status_below_cap(
    harness_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(harness_repo)
    monkeypatch.setattr(runner_recovery, "_run_llm", lambda *a, **k: None)

    ctx = _ctx_for(harness_repo, "agent-a")
    ctx.journal_entry = journal.start_session(ctx.task.task_id, "agent/t/1", ctx.base_commit)
    ctx.last_status = "mechanical"
    ctx.last_hook_log = "boom"

    assert agent_runner.autorepair(ctx) is True  # below the cap -> retry
    assert ctx.journal_entry["attempts"][-1]["status"] == "mechanical"
