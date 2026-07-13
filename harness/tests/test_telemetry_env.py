"""Edge-case tests for the shared telemetry env-parsing helpers.

``_env_float`` is the single source now used by both telemetry and the LLM seam
(step/wall-clock timeouts), so its blank/non-numeric/valid handling is pinned
here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "harness"))

import telemetry  # noqa: E402


def test_env_float_absent_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_TEST_FLOAT", raising=False)
    assert telemetry._env_float("HARNESS_TEST_FLOAT") is None


def test_env_float_blank_and_whitespace_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TEST_FLOAT", "   ")
    assert telemetry._env_float("HARNESS_TEST_FLOAT") is None


def test_env_float_non_numeric_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TEST_FLOAT", "abc")
    assert telemetry._env_float("HARNESS_TEST_FLOAT") is None


def test_env_float_valid_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TEST_FLOAT", " 1.5 ")
    assert telemetry._env_float("HARNESS_TEST_FLOAT") == 1.5


def test_env_int_valid_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TEST_INT", "42")
    assert telemetry._env_int("HARNESS_TEST_INT") == 42
    monkeypatch.setenv("HARNESS_TEST_INT", "")
    assert telemetry._env_int("HARNESS_TEST_INT") is None
    monkeypatch.setenv("HARNESS_TEST_INT", "nope")
    assert telemetry._env_int("HARNESS_TEST_INT") is None


def test_budget_utilisation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TOTAL_TOKENS", "100")
    monkeypatch.delenv("MAX_RUN_COST_USD", raising=False)
    ledger = telemetry.TokenLedger()
    ledger.record(telemetry.StepUsage(total_tokens=75))
    util = ledger.budget_utilisation()
    assert util is not None and abs(util - 0.75) < 1e-9


def test_budget_utilisation_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_TOTAL_TOKENS", raising=False)
    monkeypatch.delenv("MAX_RUN_COST_USD", raising=False)
    assert telemetry.TokenLedger().budget_utilisation() is None
