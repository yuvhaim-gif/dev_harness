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

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "harness" / "enforce_file_locks.py"
BINDING_HOOK = REPO_ROOT / "harness" / "enforce_contract_binding.py"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "harness"))

import agent_runner  # noqa: E402
import command_guard  # noqa: E402
import forensic  # noqa: E402
import git  # noqa: E402
import lock_policy  # noqa: E402
import log_condenser  # noqa: E402
import okf  # noqa: E402
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


def test_h4f_flags_hookspath_override() -> None:
    res = command_guard.sanitize_command("git -c core.hooksPath=/dev/null commit -m x")
    assert res.suspicious
    assert any("hooksPath" in f for f in res.flagged)


def test_h4g_flags_commit_tree_plumbing() -> None:
    res = command_guard.sanitize_command("git commit-tree abc123 -m x")
    assert res.suspicious
    assert any("commit-tree" in f for f in res.flagged)


def test_h4h_clean_commit_is_not_flagged() -> None:
    res = command_guard.sanitize_command('python tool.py && git commit -m "ok"')
    assert not res.suspicious
    assert not res.tampered


def test_h4i_flags_survive_unparseable_command() -> None:
    res = command_guard.sanitize_command("git -c core.hooksPath=x commit -m 'unterminated")
    assert res.suspicious


def test_h4j_strips_no_verify_after_global_C_flag() -> None:
    res = command_guard.sanitize_command("git -C /repo commit --no-verify -m x")
    assert "--no-verify" in res.stripped
    assert "--no-verify" not in res.sanitized


def test_h4k_strips_short_n_after_git_dir_value_flag() -> None:
    res = command_guard.sanitize_command("git --git-dir /repo/.git commit -n -m x")
    assert "-n" in res.stripped
    assert "-n" not in res.sanitized


def test_h4l_value_flag_does_not_consume_real_subcommand() -> None:
    res = command_guard.sanitize_command("git -c user.name=x commit --no-verify -m y")
    assert "--no-verify" in res.stripped


def test_h4m_global_flag_before_commit_tree_is_still_flagged() -> None:
    res = command_guard.sanitize_command("git -C /repo commit-tree abc -m x")
    assert any("commit-tree" in f for f in res.flagged)


def test_h4n_flags_bypass_via_command_substitution() -> None:
    res = command_guard.sanitize_command("echo $(git commit --no-verify)")
    assert res.suspicious
    assert any("obfuscated git-bypass" in f for f in res.flagged)


def test_h4o_flags_bypass_via_backticks() -> None:
    res = command_guard.sanitize_command("`git commit -m x --no-verify`")
    assert res.suspicious
    assert any("obfuscated git-bypass" in f for f in res.flagged)


def test_h4p_flags_bypass_via_shell_variable() -> None:
    res = command_guard.sanitize_command("GIT=git; $GIT commit --no-verify")
    assert res.suspicious
    assert any("obfuscated git-bypass" in f for f in res.flagged)


def test_h4q_obfuscated_bypass_is_not_double_charged() -> None:
    # A cleanly stripped flag must stay tampered-only, never also suspicious,
    # so the orchestrator charges exactly one guard penalty for it.
    res = command_guard.sanitize_command("git commit --no-verify -m x")
    assert res.tampered
    assert not res.suspicious


def test_h4r_bypass_in_other_segment_is_not_flagged() -> None:
    # Clean git + a benign `-n` in a different segment must not be mistaken for
    # an obfuscated git-bypass.
    res = command_guard.sanitize_command("echo -n hi && git commit -m ok")
    assert not res.suspicious


def test_h4s_bypass_inside_quoted_message_is_not_flagged() -> None:
    res = command_guard.sanitize_command('git commit -m "note: --no-verify is bad"')
    assert not res.suspicious
    assert not res.tampered


def test_h4t_flags_git_bypass_inside_sh_c() -> None:
    # The quoted script is one opaque token to the outer parse; the recursive
    # scan must still surface the buried bypass flag (was undetected before).
    res = command_guard.sanitize_command('sh -c "git commit --no-verify -m foo"')
    assert res.suspicious
    assert any("shell -c" in f and "--no-verify" in f for f in res.flagged)


def test_h4u_flags_git_bypass_inside_bash_lc() -> None:
    res = command_guard.sanitize_command('bash -lc "git push --no-verify origin main"')
    assert res.suspicious
    assert any("--no-verify" in f for f in res.flagged)


def test_h4v_flags_git_bypass_inside_cmd_c() -> None:
    res = command_guard.sanitize_command('cmd /c "git commit --no-verify -m x"')
    assert res.suspicious


def test_h4w_flags_plumbing_inside_sh_c() -> None:
    res = command_guard.sanitize_command('sh -c "git commit-tree abc -m x"')
    assert res.suspicious
    assert any("commit-tree" in f for f in res.flagged)


def test_h4x_benign_sh_c_is_not_flagged() -> None:
    res = command_guard.sanitize_command('sh -c "echo hello && python build.py"')
    assert not res.suspicious
    assert not res.tampered


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


class _EmitCtx:
    """Minimal RunContext stand-in for exercising the forensic emit path."""

    def __init__(self, repo_dir: str, rollback_ok: bool) -> None:
        self.repo = type("R", (), {"working_tree_dir": repo_dir})()
        self.dry_run = False
        self.forensic_written = False
        self.rollback_ok = rollback_ok
        self.git_warnings: list[str] = []


def test_h5b_emit_refreshes_rollback_ok_built_before_rollback(tmp_path: Path) -> None:
    # Build-then-emit contract: a report constructed while rollback_ok was still
    # False (i.e. before _rollback ran) must report the *real* rollback outcome
    # once emitted after the rollback flips the flag. Before the split this field
    # was frozen at build time and section 4 always read NOT CONFIRMED.
    ctx = _EmitCtx(str(tmp_path), rollback_ok=False)
    report = forensic.ForensicReport(
        task_id="t", mutation_mode="isolated", outcome="escalated", rollback_ok=False
    )

    ctx.rollback_ok = True  # _rollback succeeded after the report was built
    agent_runner._emit_forensic_report(ctx, report)

    assert ctx.forensic_written is True
    body = (tmp_path / ".harness" / "logs" / "FAILED_AGENT_RUN.md").read_text(encoding="utf-8")
    assert "Local working tree rollback: **CONFIRMED**" in body
    assert "NOT CONFIRMED" not in body

    # Idempotent: a second emit must not double-write once forensic_written is set.
    agent_runner._emit_forensic_report(ctx, report)


def test_h5c_attempts_show_distinct_per_attempt_cost() -> None:
    # Regression: _fmt_attempts must pair attempt i with the i-th autorepair
    # step rather than repeat the first step's usage on every row. The final
    # cap-exceeding attempt has no following step and honestly shows zero.
    report = forensic.ForensicReport(
        task_id="t",
        mutation_mode="isolated",
        outcome="escalated",
        attempts=[
            {"at": "t1", "state": "Enforce", "status": "semantic"},
            {"at": "t2", "state": "Enforce", "status": "semantic"},
            {"at": "t3", "state": "Enforce", "status": "cap"},
        ],
        telemetry={
            "steps": [
                {"phase": "mutate", "cost_usd": 5.0},
                {
                    "phase": "autorepair",
                    "cost_usd": 0.10,
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "total_tokens": 3,
                },
                {
                    "phase": "autorepair",
                    "cost_usd": 0.20,
                    "input_tokens": 4,
                    "output_tokens": 5,
                    "total_tokens": 9,
                },
            ],
        },
    )
    table = forensic._fmt_attempts(report.attempts, report.telemetry)
    rows = [r for r in table.splitlines() if r.startswith(("| 1 ", "| 2 ", "| 3 "))]
    assert "0.1000" in rows[0] and "1/2/3" in rows[0]  # attempt 1 -> 1st repair step
    assert "0.2000" in rows[1] and "4/5/9" in rows[1]  # attempt 2 -> 2nd repair step
    assert "0.0000" in rows[2]  # attempt 3 -> no following step, honest zero
    assert "5.0000" not in table  # the mutate step never leaks into an attempt row


def test_h5f_postmortem_is_an_okf_concept(tmp_path: Path) -> None:
    # The durable memory artifact is itself OKF-conformant (type: Postmortem)
    # so a failed run joins the knowledge layer instead of being an opaque dump.
    report = forensic.ForensicReport(
        task_id="add_payments_endpoint",
        mutation_mode="evolve",
        outcome="escalated",
        reason="cap exceeded",
        work_branch="agent/add_payments_endpoint",
    )
    path = forensic.write_okf_postmortem(report, repo_dir=str(tmp_path))
    assert Path(path).exists()
    text = Path(path).read_text(encoding="utf-8")
    doc = okf.parse_document(text)
    assert doc.frontmatter is not None and doc.frontmatter.get("type") == "Postmortem"
    # A postmortem is not a contract, so its volatile timestamp is allowed.
    assert okf.validate_concept_text(text, path=path, is_contract=False) == []


def test_h5g_append_log_is_dated_newest_first(tmp_path: Path) -> None:
    first = forensic.ForensicReport(
        task_id="t1", mutation_mode="isolated", outcome="escalated", reason="one"
    )
    second = forensic.ForensicReport(
        task_id="t2", mutation_mode="evolve", outcome="aborted", reason="two"
    )
    forensic.append_log(first, repo_dir=str(tmp_path))
    log_path = Path(forensic.append_log(second, repo_dir=str(tmp_path)))

    assert log_path.name == forensic.LOG_NAME
    body = log_path.read_text(encoding="utf-8")
    assert body.startswith(forensic.LOG_TITLE)
    # Newest entry precedes the older one in the same-day section.
    assert body.index("`t2`") < body.index("`t1`")
    # The log file is OKF-lenient (reserved name), so it always validates.
    assert okf.validate_concept_text(body, path=str(log_path), is_contract=False) == []


# --------------------------------------------------------------------------- #
# H5d. Coordination-payload validator (allowlist exemption is content-aware)
# --------------------------------------------------------------------------- #
def test_h5d_validator_accepts_flat_lease_json() -> None:
    blob = json.dumps(
        {
            "task_id": "t",
            "branch": "b",
            "agent_id": "a",
            "base_commit": "c",
            "targets": [],
            "created_at": "x",
            "ttl_seconds": 1,
        }
    )
    assert lock_policy.is_valid_coordination_payload(".harness/leases/t.json", blob)


def test_h5d_validator_accepts_flat_journal_json() -> None:
    blob = json.dumps({"task_id": "t", "outcome": "escalated"})
    assert lock_policy.is_valid_coordination_payload(".harness/journal/t.json", blob)


def test_h5d_validator_rejects_non_json_extension() -> None:
    # The core smuggling vector: an arbitrary .py under the exempt prefix.
    assert not lock_policy.is_valid_coordination_payload(".harness/journal/payload.py", "x = 1\n")


def test_h5d_validator_rejects_nested_path() -> None:
    blob = json.dumps({"task_id": "t"})
    assert not lock_policy.is_valid_coordination_payload(".harness/journal/sub/t.json", blob)


def test_h5d_validator_rejects_unknown_keys() -> None:
    assert not lock_policy.is_valid_coordination_payload(
        ".harness/journal/t.json", json.dumps({"evil": "payload"})
    )


def test_h5d_validator_rejects_non_dict_and_malformed_json() -> None:
    assert not lock_policy.is_valid_coordination_payload(".harness/leases/t.json", "[1, 2, 3]")
    assert not lock_policy.is_valid_coordination_payload(".harness/leases/t.json", "{not json")


def test_h5d_validator_rejects_non_coordination_path_and_none_blob() -> None:
    assert not lock_policy.is_valid_coordination_payload("src/x.py", "{}")
    assert not lock_policy.is_valid_coordination_payload(".harness/leases/t.json", None)


# --------------------------------------------------------------------------- #
# H5e. Immutable rules tag handover/journal content as untrusted data
# --------------------------------------------------------------------------- #
def test_h5e_static_rules_mark_handover_as_untrusted() -> None:
    rules = prompt_builder.STATIC_RULES
    assert "UNTRUSTED DATA" in rules
    assert "AGENT_HANDOVER_FILE" in rules
    # It must travel in the byte-stable static prefix, so every repair cycle and
    # every assembled prompt carries the injection guard.
    prompt = prompt_builder.build_repair_prompt(
        task={"task_id": "t", "mutation_mode": "isolated"},
        allowlist=["src/x.py"],
        condensed_log="boom",
        attempt=1,
        max_attempts=3,
    )
    assert "UNTRUSTED DATA" in prompt


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
    for rel in ("harness/example/tests", "harness/example/src/db"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    (tmp_path / "harness" / "example" / "tests" / "test_queries.py").write_text(
        "# t\n", encoding="utf-8"
    )
    (tmp_path / "harness" / "example" / "src" / "db" / "queries.py").write_text(
        "# q\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


# --------------------------------------------------------------------------- #
# H7. End-to-end financial abort -> forensic report -> safe rollback
# --------------------------------------------------------------------------- #
RUNNER = REPO_ROOT / "harness" / "agent_runner.py"

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

    # Regression: the report is *built* before _rollback but *emitted* after, so
    # section 4 must reflect the real (successful) rollback, not the stale False
    # default. Before the build/emit split this always read NOT CONFIRMED even
    # though the tree above is provably clean and back on main.
    assert "Local working tree rollback: **CONFIRMED**" in body
    assert "NOT CONFIRMED" not in body


def test_h6_skip_agent_harness_bypasses_lock_hook(seeded_repo: Path) -> None:
    target = seeded_repo / "harness" / "example" / "tests" / "test_queries.py"
    with target.open("a", encoding="utf-8") as fh:
        fh.write("# locked edit\n")
    _git(seeded_repo, "add", "harness/example/tests/test_queries.py")

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


# --------------------------------------------------------------------------- #
# H8. Out-of-band (hook-bypassed) commit is caught by the post-hoc gate
# --------------------------------------------------------------------------- #
# The LLM seam spawns its own git and commits an out-of-allowlist file while
# explicitly skipping any hooks (``core.hooksPath``). The command guard cannot
# see inside the spawned process, so the authoritative defence is the post-hoc
# containment gate, which inspects committed state and must abort with exit 4.
_ESCAPE_LLM = (
    "import subprocess\n"
    "open('escaped.py', 'w').write('x = 1\\n')\n"
    "subprocess.run(['git', 'add', 'escaped.py'])\n"
    "subprocess.run(['git', '-c', 'core.hooksPath=.', 'commit', '-m', 'out of band'])\n"
)


def test_h8_out_of_band_commit_is_contained(seeded_repo: Path) -> None:
    (seeded_repo / "escape_llm.py").write_text(_ESCAPE_LLM, encoding="utf-8")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env["AGENT_ID"] = "agent-test"
    env["AGENT_LLM_CMD"] = f'"{sys.executable}" escape_llm.py'

    res = subprocess.run(
        [sys.executable, str(RUNNER), "--task", "optimise_query_layer"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 4, combined  # CONTAINMENT_ABORT_EXIT
    assert "CONTAINMENT BREACH" in combined

    report = seeded_repo / ".harness" / "logs" / "FAILED_AGENT_RUN.md"
    assert report.exists()

    # Safely contained: back on main with a clean tracked tree.
    branch = _git(seeded_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"
    status = _git(seeded_repo, "status", "--porcelain", "-uno").stdout.strip()
    assert status == "", f"tracked files not clean after rollback: {status}"


# --------------------------------------------------------------------------- #
# H8b. Symlink file-lock bypass is rejected by mode (not just path)
# --------------------------------------------------------------------------- #
# An allowlisted path can be flipped from a regular file (mode 100644) to a
# symlink (mode 120000) aimed at a locked file. The path never leaves the
# allowlist, so the path-only gates pass it; the mode-aware check must reject it.
def _stage_symlink(repo: Path, link_path: str, target: str) -> None:
    """Stage ``link_path`` as a git symlink to ``target`` without touching disk.

    Uses plumbing (blob + ``--cacheinfo`` mode 120000) so the test is portable
    to platforms without filesystem symlink support, while producing the exact
    tree entry a real symlink would.
    """
    (repo / "_link_target").write_text(target, encoding="utf-8")
    blob = _git(repo, "hash-object", "-w", "_link_target").stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},{link_path}")


def test_h8b_symlink_paths_parser() -> None:
    raw = (
        ":100644 120000 1111111 2222222 T\tsrc/app.py\n"
        ":100644 100644 3333333 4444444 M\tsrc/keep.py\n"
        ":000000 120000 0000000 5555555 A\tsrc/new_link\n"
    )
    assert lock_policy.symlink_paths(raw) == ["src/app.py", "src/new_link"]


def test_h8c_staged_symlink_blocked_by_hook(seeded_repo: Path) -> None:
    # queries.py is in the task allowlist; alias it onto the locked AGENTS.md.
    _stage_symlink(seeded_repo, "harness/example/src/db/queries.py", "AGENTS.md")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env["AGENT_TASK_ID"] = "optimise_query_layer"

    res = subprocess.run(
        [sys.executable, str(HOOK)], cwd=str(seeded_repo), capture_output=True, text=True, env=env
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 1, combined
    assert "symlink" in combined.lower()
    assert "harness/example/src/db/queries.py" in combined


def test_h8d_committed_symlink_is_containment_breach(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(seeded_repo)
    ctx = _enforce_ctx(seeded_repo)

    _stage_symlink(seeded_repo, "harness/example/src/db/queries.py", "AGENTS.md")
    _git(seeded_repo, "-c", "core.hooksPath=.", "commit", "-m", "symlink bypass")

    # Attribute the commit to the orchestrator so the out-of-band-commit layer
    # stays quiet -- isolating the symlink (mode) detection as the sole trigger.
    ctx.runner_commits.add(ctx.repo.head.commit.hexsha)

    violations = agent_runner._containment_breach(ctx)
    assert any("symlink" in v for v in violations), violations
    assert any("harness/example/src/db/queries.py" in v for v in violations), violations


# --------------------------------------------------------------------------- #
# H9. Rollback removes agent-created untracked files (T01)
# --------------------------------------------------------------------------- #
# A containment abort must leave the original branch pristine, including the
# stray untracked file the LLM created but never staged. The sample workload
# lives under ``harness/example/``, so the old literal ``example`` pathspec
# matched nothing and the file survived rollback.
_STRAY_LLM = (
    "import subprocess\n"
    "open('stray.py', 'w').write('x = 1\\n')\n"
    "open('escaped.py', 'w').write('x = 1\\n')\n"
    "subprocess.run(['git', 'add', 'escaped.py'])\n"
    "subprocess.run(['git', '-c', 'core.hooksPath=.', 'commit', '-m', 'out of band'])\n"
)


def test_h9_rollback_removes_agent_untracked_files(seeded_repo: Path) -> None:
    (seeded_repo / "stray_llm.py").write_text(_STRAY_LLM, encoding="utf-8")
    # Commit the driver so the only untracked file at assert time is the one
    # the LLM creates -- which rollback must remove.
    _git(seeded_repo, "add", "stray_llm.py")
    _git(seeded_repo, "commit", "-m", "add driver")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env["AGENT_ID"] = "agent-test"
    env["AGENT_LLM_CMD"] = f'"{sys.executable}" stray_llm.py'

    res = subprocess.run(
        [sys.executable, str(RUNNER), "--task", "optimise_query_layer"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 4, res.stdout + res.stderr  # CONTAINMENT_ABORT_EXIT

    # The agent-created untracked file is gone, and nothing outside the
    # harness's own coordination state (.harness/ -- forensic + journals) is
    # left dirty back on the original branch.
    assert not (seeded_repo / "stray.py").exists()
    branch = _git(seeded_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"
    entries = _git(seeded_repo, "status", "--porcelain").stdout.splitlines()
    leftover = [line for line in entries if ".harness/" not in line]
    assert leftover == [], f"tree not clean after rollback: {leftover}"


def test_h9b_harness_managed_classification(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rollback keeps only the harness's own artifacts under .harness/ and removes
    # LLM-written junk there. Before the fix, _rollback skipped everything under
    # .harness/, so a stray .py or malformed json survived a "pristine" rollback.
    monkeypatch.chdir(seeded_repo)
    repo_dir = str(seeded_repo)
    harness = seeded_repo / ".harness"
    (harness / "logs").mkdir(parents=True, exist_ok=True)
    (harness / "journal").mkdir(parents=True, exist_ok=True)
    (harness / "logs" / "FAILED_AGENT_RUN.md").write_text("x\n", encoding="utf-8")
    (harness / "telemetry").mkdir(parents=True, exist_ok=True)
    (harness / "telemetry" / "usage.json").write_text("{}\n", encoding="utf-8")
    (harness / "contracts.lock").write_text("{}\n", encoding="utf-8")
    (harness / "journal" / "t.json").write_text(
        json.dumps({"task_id": "t", "outcome": "escalated"}), encoding="utf-8"
    )
    (harness / "journal" / "payload.py").write_text("x = 1\n", encoding="utf-8")
    (harness / "journal" / "bad.json").write_text("{not json", encoding="utf-8")
    (harness / "stray.py").write_text("x = 1\n", encoding="utf-8")

    # Kept (managed): logs, telemetry, manifest, and a well-formed journal blob.
    assert agent_runner._is_harness_managed(repo_dir, ".harness/logs/FAILED_AGENT_RUN.md")
    assert agent_runner._is_harness_managed(repo_dir, ".harness/telemetry/usage.json")
    assert agent_runner._is_harness_managed(repo_dir, ".harness/contracts.lock")
    assert agent_runner._is_harness_managed(repo_dir, ".harness/journal/t.json")
    # Removed (not managed): a smuggled .py, malformed json, or a stray file
    # outside the managed subtrees -- all junk rollback must clean up.
    assert not agent_runner._is_harness_managed(repo_dir, ".harness/journal/payload.py")
    assert not agent_runner._is_harness_managed(repo_dir, ".harness/journal/bad.json")
    assert not agent_runner._is_harness_managed(repo_dir, ".harness/stray.py")


# --------------------------------------------------------------------------- #
# H10. Commit classification keys off worktree state, not hook wording (T03)
# --------------------------------------------------------------------------- #
class _FakeCommit:
    """Stand-in for a blocked `git commit` subprocess result."""

    returncode = 1
    stdout = ""
    stderr = "a hook rejected the commit (wording the harness must NOT parse)"


def _enforce_ctx(repo_path: Path) -> agent_runner.RunContext:
    repo = git.Repo(str(repo_path))
    repo.git.checkout("-b", "agent/optimise_query_layer/test")
    task = agent_runner._parse_task("optimise_query_layer")
    return agent_runner.RunContext(
        repo=repo,
        task=task,
        dry_run=False,
        agent_id="a",
        base_commit=repo.head.commit.hexsha,
    )


def test_h10a_dirty_worktree_after_block_is_mechanical(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(seeded_repo)
    ctx = _enforce_ctx(seeded_repo)
    # An auto-fixer-style change left in the worktree: a blocked commit that
    # dirtied the tree must be classified mechanical regardless of hook wording.
    (seeded_repo / "harness" / "example" / "src" / "db" / "queries.py").write_text(
        "# q\nx = 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(agent_runner.subprocess, "run", lambda *a, **k: _FakeCommit())
    status, _ = agent_runner.enforce(ctx)
    assert status == "mechanical"


def test_h10b_clean_worktree_after_block_is_semantic(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(seeded_repo)
    ctx = _enforce_ctx(seeded_repo)
    # Nothing changed on disk: a blocked commit that left the tree as staged is
    # a genuine semantic/lock rejection.
    monkeypatch.setattr(agent_runner.subprocess, "run", lambda *a, **k: _FakeCommit())
    status, _ = agent_runner.enforce(ctx)
    assert status == "semantic"


# --------------------------------------------------------------------------- #
# H11. LLM seam env scoping + exposure visibility (T05)
# --------------------------------------------------------------------------- #
def test_h11a_allowlist_scopes_seam_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ENV_ALLOWLIST", "FOO, BAR")
    monkeypatch.setenv("FOO", "1")
    monkeypatch.setenv("BAR", "2")
    monkeypatch.setenv("SECRET", "leak")
    monkeypatch.setenv("AGENT_CUSTOM", "kept")  # AGENT_* keys carry through

    env = agent_runner._seam_base_env()
    assert env.get("FOO") == "1"
    assert env.get("BAR") == "2"
    assert env.get("AGENT_CUSTOM") == "kept"
    assert "SECRET" not in env


def test_h11b_no_allowlist_is_full_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_ENV_ALLOWLIST", raising=False)
    monkeypatch.setenv("SECRET", "inherited")
    env = agent_runner._seam_base_env()
    assert env.get("SECRET") == "inherited"  # default behaviour unchanged


def test_h11e_skip_override_never_inherited_by_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    # The human override must not reach the agent's subprocess in either mode,
    # or it could disable the local lock hook for git commands the agent spawns.
    monkeypatch.setenv("SKIP_AGENT_HARNESS", "1")

    monkeypatch.delenv("AGENT_ENV_ALLOWLIST", raising=False)
    assert "SKIP_AGENT_HARNESS" not in agent_runner._seam_base_env()  # full-copy mode

    monkeypatch.setenv("AGENT_ENV_ALLOWLIST", "SKIP_AGENT_HARNESS")  # even if allowlisted
    assert "SKIP_AGENT_HARNESS" not in agent_runner._seam_base_env()


def test_h11c_missing_allowlist_warns_once_and_reports_full_copy(seeded_repo: Path) -> None:
    (seeded_repo / "fake_llm.py").write_text(_FAKE_LLM, encoding="utf-8")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env.pop("AGENT_ENV_ALLOWLIST", None)
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
    assert res.returncode == 3, combined
    # The exposure is surfaced exactly once at the seam.
    assert combined.count("AGENT_ENV_ALLOWLIST not set") == 1

    body = (seeded_repo / ".harness" / "logs" / "FAILED_AGENT_RUN.md").read_text(encoding="utf-8")
    assert "`full_copy`" in body  # forensic env_scope audit field
    assert "AGENT_ENV_ALLOWLIST not set" in body  # recorded as a git warning


def test_h11d_doctor_reports_env_scope(seeded_repo: Path) -> None:
    env = os.environ.copy()
    env["AGENT_ENV_ALLOWLIST"] = "FOO"
    scoped = subprocess.run(
        [sys.executable, str(RUNNER), "--doctor"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert "env_scope=allowlisted" in scoped.stdout

    env.pop("AGENT_ENV_ALLOWLIST", None)
    unscoped = subprocess.run(
        [sys.executable, str(RUNNER), "--doctor"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert "env_scope=full_copy" in unscoped.stdout


# --------------------------------------------------------------------------- #
# H12. Step timeout aborts with a timeout reason distinct from a budget abort (T08)
# --------------------------------------------------------------------------- #
# Sleeps well past AGENT_STEP_TIMEOUT_SECONDS so the per-step timeout always
# fires first. Kept short because, with captured output, the orphaned child
# holds the pipe until it exits on its own.
_SLEEP_LLM = "import time\ntime.sleep(5)\n"


def test_h12_step_timeout_aborts_distinct_from_financial(seeded_repo: Path) -> None:
    (seeded_repo / "sleep_llm.py").write_text(_SLEEP_LLM, encoding="utf-8")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env["AGENT_ID"] = "agent-test"
    env["AGENT_LLM_CMD"] = f'"{sys.executable}" sleep_llm.py'
    env["AGENT_STEP_TIMEOUT_SECONDS"] = "1"

    res = subprocess.run(
        [sys.executable, str(RUNNER), "--task", "optimise_query_layer"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 3, combined  # BUDGET_ABORT_EXIT family, but a timeout
    assert "TIMEOUT ABORT" in combined
    assert "FINANCIAL ABORT" not in combined

    body = (seeded_repo / ".harness" / "logs" / "FAILED_AGENT_RUN.md").read_text(encoding="utf-8")
    assert "step timeout" in body.lower()
    assert "financial abort" not in body.lower()

    # Safely contained: back on main with a clean tracked tree.
    branch = _git(seeded_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "main"


# --------------------------------------------------------------------------- #
# H13. Guard penalties have their own budget, separate from autorepair (T10)
# --------------------------------------------------------------------------- #
def test_h13a_guard_penalty_does_not_charge_autorepair(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(seeded_repo)
    ctx = _enforce_ctx(seeded_repo)
    # A git bypass flag on a commit/push must charge guard_penalties, not the
    # legitimate autorepair budget. The flag is stripped, so the sanitized
    # `git commit -m noop` is a harmless no-op on the clean fixture tree.
    monkeypatch.setenv("AGENT_LLM_CMD", "git commit --no-verify -m noop")
    agent_runner._run_llm(ctx, "mutate")
    assert ctx.guard_penalties == 1
    assert ctx.autorepair_attempts == 0


# A no-op LLM keeps the tree unchanged, so enforce finds nothing to commit and
# the loop keeps cycling -- charging a fresh guard penalty each mutate/autorepair
# until the guard ceiling is crossed.
_NOOP_LLM = "x = 0\n"


def test_h13b_guard_ceiling_exits_four_with_bypass_reason(seeded_repo: Path) -> None:
    (seeded_repo / "noop_llm.py").write_text(_NOOP_LLM, encoding="utf-8")

    env = os.environ.copy()
    env.pop("SKIP_AGENT_HARNESS", None)
    env["AGENT_ID"] = "agent-test"
    env["AGENT_LLM_CMD"] = f'"{sys.executable}" noop_llm.py && git commit --no-verify -m noop'

    res = subprocess.run(
        [sys.executable, str(RUNNER), "--task", "optimise_query_layer"],
        cwd=str(seeded_repo),
        capture_output=True,
        text=True,
        env=env,
    )
    combined = res.stdout + res.stderr
    assert res.returncode == 4, combined  # CONTAINMENT_ABORT_EXIT, not the budget family
    assert "GUARD ABORT" in combined

    body = (seeded_repo / ".harness" / "logs" / "FAILED_AGENT_RUN.md").read_text(encoding="utf-8")
    assert "git-bypass" in body.lower()
    assert "financial abort" not in body.lower()
    assert "autorepair cap" not in body.lower()


# --------------------------------------------------------------------------- #
# H14. OKF info-layer breach is caught by the post-hoc containment gate
# --------------------------------------------------------------------------- #
# An agent that strips the OKF frontmatter off a committed spec_doc keeps the
# path inside its allowlist, so the path/symlink gates pass it. The info-layer
# re-check must still flag the malformed concept as a containment breach.
def _okf_ctx(repo_path: Path) -> tuple[agent_runner.RunContext, Path]:
    repo = git.Repo(str(repo_path))
    repo.git.checkout("-b", "agent/add_payments_endpoint/test")
    base = repo.head.commit.hexsha
    docs = repo_path / "harness" / "example" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    schema = docs / "API_SCHEMA.md"
    schema.write_text("---\ntype: API Contract\ntitle: X\n---\n\nbody\n", encoding="utf-8")
    task = agent_runner._parse_task("add_payments_endpoint")
    ctx = agent_runner.RunContext(
        repo=repo, task=task, dry_run=False, agent_id="a", base_commit=base
    )
    return ctx, schema


def test_h14_stripped_frontmatter_is_okf_violation(seeded_repo: Path) -> None:
    ctx, schema = _okf_ctx(seeded_repo)
    schema.write_text("# API Schema\n\nno frontmatter here\n", encoding="utf-8")
    _git(seeded_repo, "add", "-A")
    _git(seeded_repo, "commit", "-m", "strip okf frontmatter")

    problems = agent_runner._okf_violations(ctx)
    assert any("API_SCHEMA.md" in p for p in problems), problems
    breaches = agent_runner._containment_breach(ctx)
    assert any("OKF info-layer violation" in b for b in breaches), breaches


def test_h14b_conformant_spec_doc_is_not_a_violation(seeded_repo: Path) -> None:
    ctx, _ = _okf_ctx(seeded_repo)
    _git(seeded_repo, "add", "-A")
    _git(seeded_repo, "commit", "-m", "add conformant spec doc")

    assert agent_runner._okf_violations(ctx) == []
