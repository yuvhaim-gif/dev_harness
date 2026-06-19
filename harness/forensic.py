#!/usr/bin/env python3
"""Forensic post-mortem reporting for rejected or crashed agent runs.

When a run is escalated, financially aborted, or crashes, the harness compiles a
transparent audit at ``.harness/logs/FAILED_AGENT_RUN.md`` and prints a terminal
status badge. The report answers, at a glance:

  1. allowed scope vs. paths actually modified (the containment proof),
  2. the terminal error codes, failing assertions, and git policy warnings,
  3. a chronological step log with token consumption + cost per attempt, and
  4. confirmation the local working tree was safely rolled back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

REPORT_DIR = ".harness/logs"
REPORT_NAME = "FAILED_AGENT_RUN.md"


@dataclass
class ForensicReport:
    task_id: str
    mutation_mode: str
    outcome: str
    reason: str = ""
    base_commit: str = ""
    work_branch: str = ""
    error_code: int | None = None
    allowed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    failure_excerpt: str = ""
    git_warnings: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    telemetry: dict[str, Any] = field(default_factory=dict)
    rollback_ok: bool = False


def report_path(repo_dir: str = ".", report_dir: str = REPORT_DIR) -> str:
    return os.path.join(repo_dir, report_dir, REPORT_NAME)


def _fmt_list(items: list[str]) -> str:
    if not items:
        return "- _(none)_\n"
    return "".join(f"- `{item}`\n" for item in items)


def _fmt_attempts(attempts: list[dict[str, Any]], telemetry: dict[str, Any]) -> str:
    steps = telemetry.get("steps") or []
    by_phase: dict[str, dict[str, Any]] = {}
    for step in steps:
        by_phase.setdefault(str(step.get("phase", "")), step)

    if not attempts:
        return "_No autorepair attempts were recorded._\n"

    rows = [
        "| # | at | state | status | tokens (in/out/total) | cost (USD) |",
        "|---|----|-------|--------|-----------------------|------------|",
    ]
    for idx, att in enumerate(attempts, start=1):
        usage = by_phase.get("autorepair") or {}
        tokens = (
            f"{usage.get('input_tokens', 0)}/"
            f"{usage.get('output_tokens', 0)}/"
            f"{usage.get('total_tokens', 0)}"
        )
        cost = f"{float(usage.get('cost_usd', 0.0)):.4f}"
        rows.append(
            f"| {idx} | {att.get('at', '')} | {att.get('state', '')} "
            f"| {att.get('status', '')} | {tokens} | {cost} |"
        )
    return "\n".join(rows) + "\n"


def render(report: ForensicReport) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tel = report.telemetry
    lines: list[str] = []
    lines.append("# FAILED AGENT RUN")
    lines.append("")
    lines.append(f"- **generated:** {now}")
    lines.append(f"- **task:** `{report.task_id}` (mode: `{report.mutation_mode}`)")
    lines.append(f"- **outcome:** `{report.outcome}`")
    if report.reason:
        lines.append(f"- **reason:** {report.reason}")
    lines.append(f"- **work branch:** `{report.work_branch or '(none)'}`")
    lines.append(f"- **base commit:** `{report.base_commit or '(none)'}`")
    if report.error_code is not None:
        lines.append(f"- **terminal exit code:** `{report.error_code}`")
    lines.append("")

    lines.append("## 1. Scope vs. Modified")
    lines.append("")
    lines.append("**Allowed scope:**")
    lines.append(_fmt_list(sorted(report.allowed)).rstrip("\n"))
    lines.append("")
    lines.append("**Actually modified:**")
    lines.append(_fmt_list(sorted(report.modified)).rstrip("\n"))
    lines.append("")
    lines.append("**Out-of-scope modifications (containment breach attempts):**")
    lines.append(_fmt_list(sorted(report.out_of_scope)).rstrip("\n"))
    lines.append("")

    lines.append("## 2. Errors, Assertions & Policy Warnings")
    lines.append("")
    if report.git_warnings:
        lines.append("**Git policy warnings:**")
        lines.append(_fmt_list(report.git_warnings).rstrip("\n"))
        lines.append("")
    lines.append("**Failure excerpt (condensed):**")
    lines.append("")
    lines.append("```")
    lines.append(report.failure_excerpt.strip() or "(no failure log captured)")
    lines.append("```")
    lines.append("")

    lines.append("## 3. Chronological Step Log (tokens & cost)")
    lines.append("")
    lines.append(_fmt_attempts(report.attempts, tel).rstrip("\n"))
    lines.append("")
    lines.append(
        "**Run totals:** "
        f"tokens in/out/total = {tel.get('input_tokens', 0)}/"
        f"{tel.get('output_tokens', 0)}/{tel.get('total_tokens', 0)}; "
        f"cost = ${float(tel.get('cost_usd', 0.0)):.4f}."
    )
    lines.append("")

    lines.append("## 4. Rollback Verification")
    lines.append("")
    status = "CONFIRMED" if report.rollback_ok else "NOT CONFIRMED"
    lines.append(f"- Local working tree rollback: **{status}**.")
    lines.append("")
    return "\n".join(lines)


def write_report(report: ForensicReport, repo_dir: str = ".") -> str:
    path = report_path(repo_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(report))
    return path


def print_badge(outcome: str, path: str) -> None:
    bar = "=" * 64
    print(bar)
    print(f"  [HARNESS] AGENT RUN {outcome.upper()} -- workspace safely contained")
    print(f"  Forensic report: {path}")
    print(bar)
