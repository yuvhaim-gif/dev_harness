#!/usr/bin/env python3
"""Token and cost telemetry for the agent loop.

The orchestrator drives a provider-agnostic ``AGENT_LLM_CMD``. To keep a run
inside its financial and context limits, the command is asked to write a
structured usage payload to ``AGENT_TOKEN_USAGE_FILE`` after each step. This
module reads that payload, normalises the many provider field spellings into one
shape, accumulates a running ledger, and answers a single question: has any
configured budget been exceeded?

Budgets (environment, all optional):
    MAX_TOTAL_TOKENS    -- hard ceiling on cumulative total tokens
    MAX_RUN_COST_USD    -- hard ceiling on cumulative USD cost

When a payload omits an explicit cost, it is derived from per-1K pricing when
configured:
    AGENT_COST_PER_1K_INPUT, AGENT_COST_PER_1K_OUTPUT

Everything degrades gracefully: a missing file, malformed JSON, or absent
budgets simply yields zero usage and no abort, so dry runs and tests that do not
wire an LLM stay green.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_USAGE_FILE = ".harness/telemetry/usage.json"

_INPUT_KEYS = ("input_tokens", "prompt_tokens", "input", "prompt")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output", "completion")
_TOTAL_KEYS = ("total_tokens", "total")
_COST_KEYS = ("cost_usd", "cost", "usd")
_NESTED_KEYS = ("tool_token_usage", "token_usage", "usage", "response", "metadata", "data")


def usage_file_path() -> str:
    return os.getenv("AGENT_TOKEN_USAGE_FILE") or DEFAULT_USAGE_FILE


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _dig(payload: dict[str, Any]) -> dict[str, Any]:
    """Find the dict that actually carries usage fields.

    Providers nest usage under varied envelopes (``usage``, ``token_usage``,
    ``tool_token_usage`` ...). Walk one level of known envelopes and prefer the
    first that exposes any recognised token key.
    """
    flat_keys = _INPUT_KEYS + _OUTPUT_KEYS + _TOTAL_KEYS + _COST_KEYS
    if any(key in payload for key in flat_keys):
        return payload
    for key in _NESTED_KEYS:
        nested = payload.get(key)
        if isinstance(nested, dict) and any(k in nested for k in flat_keys):
            return nested
    return payload


def _derive_cost(input_tokens: int, output_tokens: int) -> float:
    cost = 0.0
    per_in = _env_float("AGENT_COST_PER_1K_INPUT")
    per_out = _env_float("AGENT_COST_PER_1K_OUTPUT")
    if per_in:
        cost += (input_tokens / 1000.0) * per_in
    if per_out:
        cost += (output_tokens / 1000.0) * per_out
    return cost


@dataclass
class StepUsage:
    phase: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


def parse_payload(data: Any, phase: str = "") -> StepUsage:
    if not isinstance(data, dict):
        return StepUsage(phase=phase)
    src = _dig(data)
    input_tokens = _coerce_int(_first(src, _INPUT_KEYS))
    output_tokens = _coerce_int(_first(src, _OUTPUT_KEYS))
    total_raw = _first(src, _TOTAL_KEYS)
    total_tokens = _coerce_int(total_raw) if total_raw is not None else input_tokens + output_tokens
    cost_raw = _first(src, _COST_KEYS)
    cost_usd = (
        _coerce_float(cost_raw)
        if cost_raw is not None
        else _derive_cost(input_tokens, output_tokens)
    )
    return StepUsage(
        phase=phase,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


def read_step_usage(path: str | None = None, phase: str = "") -> StepUsage | None:
    """Read and parse the per-step usage file. None when absent/unreadable."""
    target = path or usage_file_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data: Any = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return parse_payload(data, phase=phase)


def clear_usage_file(path: str | None = None) -> None:
    """Remove a stale usage file so the next step's payload is unambiguous."""
    target = path or usage_file_path()
    try:
        os.remove(target)
    except FileNotFoundError:
        return
    except OSError:
        return


@dataclass
class TokenLedger:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)

    def record(self, step: StepUsage) -> None:
        self.input_tokens += step.input_tokens
        self.output_tokens += step.output_tokens
        self.total_tokens += step.total_tokens
        self.cost_usd += step.cost_usd
        self.steps.append(asdict(step))

    def record_from_file(self, phase: str, path: str | None = None) -> StepUsage | None:
        step = read_step_usage(path, phase=phase)
        if step is not None:
            self.record(step)
        return step

    def exceeded(self) -> str | None:
        """Return a human-readable reason if any configured budget is breached."""
        max_tokens = _env_int("MAX_TOTAL_TOKENS")
        if max_tokens is not None and self.total_tokens > max_tokens:
            return f"token budget exceeded: {self.total_tokens} > {max_tokens} (MAX_TOTAL_TOKENS)"
        max_cost = _env_float("MAX_RUN_COST_USD")
        if max_cost is not None and self.cost_usd > max_cost:
            return (
                f"cost budget exceeded: ${self.cost_usd:.4f} > ${max_cost:.4f} (MAX_RUN_COST_USD)"
            )
        return None

    def budget_utilisation(self) -> float | None:
        """Max fraction (0..1) of any configured budget currently consumed."""
        fracs: list[float] = []
        max_tokens = _env_int("MAX_TOTAL_TOKENS")
        if max_tokens:
            fracs.append(self.total_tokens / max_tokens)
        max_cost = _env_float("MAX_RUN_COST_USD")
        if max_cost:
            fracs.append(self.cost_usd / max_cost)
        return max(fracs) if fracs else None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"tokens(in={self.input_tokens}, out={self.output_tokens}, "
            f"total={self.total_tokens}), cost=${self.cost_usd:.4f}, "
            f"steps={len(self.steps)}"
        )
