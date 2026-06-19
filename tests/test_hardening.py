"""Tests for the hardening layer added on top of the base harness.

Covers token/cost telemetry budgets, log condensation, cache-ordered prompt
construction, git escape-hatch interception, the forensic post-mortem report,
and the SKIP_AGENT_HARNESS human override on the gating hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "harness" / "enforce_file_locks.py"
BINDING_HOOK = REPO_ROOT / "harness" / "enforce_contract_binding.py"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "harness"))

import command_guard  # noqa: E402
import forensic  # noqa: E402
import log_condenser  # noqa: E402
import prompt_builder  # noqa: E402
import telemetry  # noqa: E402


# --------------------------------------------------------------------------- #
# H1. Token & cost telemetry
# --------------------------------------------------------------------------- #
def test_h1a_parse_normalises_provider_fields() -> None:
    step = telemetry.parse_payload(
        {"usage": {"prompt_tokens": 100, "completion_tokens": 40}}, phase="mutate"
    )
    assert step.input_tokens == 100
    assert step.output_tokens == 40
    assert step.total_tokens == 140  # derived when absent
    assert step.phase == "mutate"


def test_h1b_explicit_total_and_cost_respected() -> None:
    step = telemetry.parse_payload(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 999, "cost_usd": 1.25}
    )
    assert step.total_tokens == 999
    assert step.cost_usd == pytest.approx(1.25)


def test_h1c_cost_derived_from_pricing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_COST_PER_1K_INPUT", "1.0")
    monkeypatch.setenv("AGENT_COST_PER_1K_OUTPUT", "2.0")
    step = telemetry.parse_payload({"input_tokens": 1000, "output_tokens": 500})
    assert step.cost_usd == pytest.approx(1.0 + 1.0)  # 1*1.0 + 0.5*2.0


def test_h1d_ledger_accumulates_and_detects_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_TOTAL_TOKENS", "100")
    ledger = telemetry.TokenLedger()
    ledger.record(telemetry.StepUsage(total_tokens=60))
    assert ledger.exceeded() is None
    ledger.record(telemetry.StepUsage(total_tokens=60))
    reason = ledger.exceeded()
    assert reason is not None
    assert "MAX_TOTAL_TOKENS" in reason


def test_h1e_ledger_detects_cost_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_TOTAL_TOKENS", raising=False)
    monkeypatch.setenv("MAX_RUN_COST_USD", "0.50")
    ledger = telemetry.TokenLedger()
    ledger.record(telemetry.StepUsage(cost_usd=0.75))
    reason = ledger.exceeded()
    assert reason is not None
    assert "MAX_RUN_COST_USD" in reason


def test_h1f_read_and_clear_usage_file(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"total_tokens": 42}), encoding="utf-8")
    step = telemetry.read_step_usage(str(path), phase="autorepair")
    assert step is not None and step.total_tokens == 42
    telemetry.clear_usage_file(str(path))
    assert not path.exists()
    assert telemetry.read_step_usage(str(path)) is None


# --------------------------------------------------------------------------- #
# H2. Log condensation
# --------------------------------------------------------------------------- #
def test_h2a_extracts_pytest_assertion_and_drops_noise() -> None:
    raw = (
        "Requirement already satisfied: pip in site-packages\n"
        "platform win32 -- Python 3.12\n"
        "E       assert 400 == 201\n"
        "1 failed, 3 passed\n"
    )
    out = log_condenser.condense(raw)
    assert "assert 400 == 201" in out
    assert "site-packages" not in out
    assert "Requirement already satisfied" not in out


def test_h2b_extracts_mypy_error_with_source_context(tmp_path: Path) -> None:
    src = tmp_path / "mod.py"
    src.write_text("a = 1\nb: int = 'x'\nc = 3\n", encoding="utf-8")
    raw = "mod.py:2: error: Incompatible types in assignment\n"
    out = log_condenser.condense(raw, repo_dir=str(tmp_path))
    assert "Incompatible types" in out
    assert "mod.py:2" in out
    assert ">> 2:" in out  # the offending line is marked


def test_h2c_empty_input_stays_empty() -> None:
    assert log_condenser.condense("") == ""
    assert log_condenser.condense("   \n  ") == ""


def test_h2d_falls_back_to_tail_when_unstructured() -> None:
    raw = "\n".join(f"line {i}" for i in range(50))
    out = log_condenser.condense(raw)
    assert "LOG TAIL" in out
    assert "line 49" in out


def test_h2e_output_is_bounded() -> None:
    raw = "E       " + ("x" * 5000) + "\n"
    out = log_condenser.condense(raw, max_chars=200)
    assert len(out) <= 200


# --------------------------------------------------------------------------- #
# H3. Cache-ordered prompt builder
# --------------------------------------------------------------------------- #
def test_h3a_prompt_is_ordered_static_then_semi_then_dynamic() -> None:
    task = {
        "task_id": "t",
        "mutation_mode": "isolated",
        "description": "do x",
        "targets": ["src/x.py"],
    }
    prompt = prompt_builder.build_repair_prompt(
        task=task,
        allowlist=["src/x.py"],
        condensed_log="E assert fail",
        attempt=1,
        max_attempts=3,
        diff="--- a\n+++ b",
        metrics="tokens=10",
    )
    i_static = prompt.index("IMMUTABLE RULES")
    i_semi = prompt.index("TASK CONTRACT")
    i_dyn = prompt.index("CURRENT FAILURE")
    assert i_static < i_semi < i_dyn
    assert "src/x.py" in prompt
    assert "E assert fail" in prompt


def test_h3b_write_prompt_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "prompt.txt"
    written = prompt_builder.write_prompt("hello", str(path))
    assert Path(written).read_text(encoding="utf-8").startswith("hello")


# --------------------------------------------------------------------------- #
# H4. Git escape-hatch interception
# --------------------------------------------------------------------------- #
def test_h4a_strips_no_verify_after_git_commit() -> None:
    res = command_guard.sanitize_command('git commit -m "x" --no-verify')
    assert res.tampered
    assert "--no-verify" in res.stripped
    assert "--no-verify" not in res.sanitized


def test_h4b_strips_short_n_after_git_push() -> None:
    res = command_guard.sanitize_command("git push -n origin main")
    assert res.tampered
    assert "-n" in res.stripped


def test_h4c_leaves_non_git_n_untouched() -> None:
    res = command_guard.sanitize_command("echo -n hello")
    assert not res.tampered
    assert res.sanitized == "echo -n hello"


def test_h4d_clean_command_is_byte_identical() -> None:
    cmd = 'python tool.py --task "add" && git commit -m "ok"'
    res = command_guard.sanitize_command(cmd)
    assert not res.tampered
    assert res.sanitized == cmd


def test_h4e_strips_only_in_git_segment() -> None:
    res = command_guard.sanitize_command("echo -n hi && git commit --no-verify")
    assert res.tampered
    assert "-n hi" in res.sanitized
    assert "--no-verify" not in res.sanitized


# --------------------------------------------------------------------------- #
# H5. Forensic post-mortem
# --------------------------------------------------------------------------- #
def test_h5a_report_has_all_four_sections_and_breach(tmp_path: Path) -> None:
    report = forensic.ForensicReport(
        task_id="t",
        mutation_mode="isolated",
        outcome="escalated",
        reason="cap exceeded",
        allowed=["src/x.py"],
        modified=["src/x.py", "src/secret.py"],
        out_of_scope=["src/secret.py"],
        failure_excerpt="E assert fail",
        telemetry={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3, "cost_usd": 0.1},
        rollback_ok=True,
    )
    text = forensic.render(report)
    assert "## 1. Scope vs. Modified" in text
    assert "## 2. Errors, Assertions & Policy Warnings" in text
    assert "## 3. Chronological Step Log" in text
    assert "## 4. Rollback Verification" in text
    assert "src/secret.py" in text
    assert "CONFIRMED" in text

    path = forensic.write_report(report, repo_dir=str(tmp_path))
    assert Path(path).exists()
    assert Path(path).name == "FAILED_AGENT_RUN.md"


# --------------------------------------------------------------------------- #
# H6. Human override switch on the gating hooks
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture()
def seeded_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "h@example.com")
    _git(tmp_path, "config", "user.name", "H")
    (tmp_path / "AGENTS.md").write_text(
        (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for rel in ("example/tests", "example/src/db"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    (tmp_path / "example" / "tests" / "test_queries.py").write_text("# t\n", encoding="utf-8")
    (tmp_path / "example" / "src" / "db" / "queries.py").write_text("# q\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


# --------------------------------------------------------------------------- #
# H7. End-to-end financial abort -> forensic report -> safe rollback
# --------------------------------------------------------------------------- #
RUNNER = REPO_ROOT / "agent_runner.py"

_FAKE_LLM = (
    "import os, json\n"
    "p = os.environ['AGENT_TOKEN_USAGE_FILE']\n"
    "os.makedirs(os.path.dirname(p) or '.', exist_ok=True)\n"
    "open(p, 'w').write(json.dumps({'total_tokens': 100000, 'cost_usd': 0.0}))\n"
)


def test_h7_financial_abort_writes_forensic_and_rolls_back(seeded_repo: Path) -> None:
    (seeded_repo / "fake_llm.py").write_text(_FAKE_LLM, encoding="utf-8")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env["AGENT_ID"] = "agent-test"
    env["AGENT_LLM_CMD"] = f'"{sys.executable}" fake_llm.py'
    env["MAX_TOTAL_TOKENS"] = "10"

    res = subprocess.run(
        [sys.executable, str(RUNNER), "--task", "optimise_query_layer"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 3, combined  # BUDGET_ABORT_EXIT
    assert "FINANCIAL ABORT" in combined

    report = seeded_repo / ".harness" / "logs" / "FAILED_AGENT_RUN.md"
    assert report.exists()
    body = report.read_text(encoding="utf-8")
    assert "financial abort" in body.lower()
    assert "Rollback Verification" in body

    # The workspace is safely contained: back on main with a clean tree.
    branch = _git(seeded_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"
    status = _git(seeded_repo, "status", "--porcelain", "-uno").stdout.strip()
    assert status == "", f"tracked files not clean after rollback: {status}"


def test_h6_skip_agent_harness_bypasses_lock_hook(seeded_repo: Path) -> None:
    with (seeded_repo / "example" / "tests" / "test_queries.py").open("a", encoding="utf-8") as fh:
        fh.write("# locked edit\n")
    _git(seeded_repo, "add", "example/tests/test_queries.py")

    env = os.environ.copy()
    env["AGENT_TASK_ID"] = "optimise_query_layer"  # would normally block this path

    blocked = subprocess.run(
        [sys.executable, str(HOOK)], cwd=str(seeded_repo), capture_output=True, text=True, env=env
    )
    assert blocked.returncode == 1

    env["SKIP_AGENT_HARNESS"] = "1"
    overridden = subprocess.run(
        [sys.executable, str(HOOK)], cwd=str(seeded_repo), capture_output=True, text=True, env=env
    )
    assert overridden.returncode == 0, overridden.stdout + overridden.stderr
    assert "human override" in overridden.stdout
