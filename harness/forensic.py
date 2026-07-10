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
POSTMORTEM_DIR = ".harness/logs/postmortems"
LOG_NAME = "log.md"
LOG_TITLE = "# Agent Run Log"


@dataclass
class ForensicReport:
    task_id: str
    mutation_mode: str
    outcome: str
    reason: str = ""
    base_commit: str = ""
    work_branch: str = ""
    work_commit: str = ""
    work_diffstat: str = ""
    error_code: int | None = None
    allowed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    failure_excerpt: str = ""
    git_warnings: list[str] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    telemetry: dict[str, Any] = field(default_factory=dict)
    rollback_ok: bool = False
    env_scope: str = "full_copy"


def report_path(repo_dir: str = ".", report_dir: str = REPORT_DIR) -> str:
    return os.path.join(repo_dir, report_dir, REPORT_NAME)


def work_patch_path(work_branch: str, repo_dir: str = ".", report_dir: str = REPORT_DIR) -> str:
    return os.path.join(repo_dir, report_dir, f"{_slug(work_branch or 'work')}.patch")


def _fmt_list(items: list[str]) -> str:
    if not items:
        return "- _(none)_\n"
    return "".join(f"- `{item}`\n" for item in items)


def _fmt_attempts(attempts: list[dict[str, Any]], telemetry: dict[str, Any]) -> str:
    # Each enforce attempt is followed by exactly one autorepair LLM step (the
    # repair it triggered), recorded in order, so attempt i pairs with the i-th
    # autorepair step. The final, cap-exceeding attempt has no following step and
    # honestly shows zero usage rather than repeating an earlier attempt's cost.
    steps = telemetry.get("steps") or []
    repair_steps = [s for s in steps if str(s.get("phase", "")) == "autorepair"]

    if not attempts:
        return "_No autorepair attempts were recorded._\n"

    rows = [
        "| # | at | state | status | tokens (in/out/total) | cost (USD) |",
        "|---|----|-------|--------|-----------------------|------------|",
    ]
    for idx, att in enumerate(attempts):
        usage = repair_steps[idx] if idx < len(repair_steps) else {}
        tokens = (
            f"{usage.get('input_tokens', 0)}/"
            f"{usage.get('output_tokens', 0)}/"
            f"{usage.get('total_tokens', 0)}"
        )
        cost = f"{float(usage.get('cost_usd', 0.0)):.4f}"
        rows.append(
            f"| {idx + 1} | {att.get('at', '')} | {att.get('state', '')} "
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

    if report.work_commit or report.work_diffstat:
        lines.append("**Work-branch snapshot (captured before the branch was deleted):**")
        lines.append("")
        if report.work_commit:
            lines.append(f"- dropped tip commit: `{report.work_commit}`")
        if report.work_diffstat:
            rel_patch = f"{REPORT_DIR}/{_slug(report.work_branch or 'work')}.patch"
            lines.append(f"- full patch: `{rel_patch}` (inspect: `git apply --stat {rel_patch}`)")
            lines.append("")
            lines.append("```")
            lines.append(report.work_diffstat.strip())
            lines.append("```")
        lines.append("")

    lines.append("## 2. Errors, Assertions & Policy Warnings")
    lines.append("")
    if report.git_warnings:
        lines.append("**Git policy warnings:**")
        lines.append(_fmt_list(report.git_warnings).rstrip("\n"))
        lines.append("")
    lines.append(f"**LLM seam env scope:** `{report.env_scope}`")
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


def write_work_patch(report: ForensicReport, patch_text: str, repo_dir: str = ".") -> str | None:
    """Persist the full work-branch diff next to the report before deletion.

    Returns the path written, or ``None`` when there is nothing to capture. The
    patch lives under the rollback-surviving ``.harness/logs/`` tree, so the
    agent's attempted changes remain inspectable as plain text even after the
    work branch ref is force-deleted -- unlike a dangling commit, this is immune
    to ``git gc``.
    """
    if not patch_text.strip():
        return None
    path = work_patch_path(report.work_branch, repo_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    body = patch_text if patch_text.endswith("\n") else patch_text + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return path


def _slug(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in value)
    return safe.strip("-") or "run"


def render_okf_postmortem(report: ForensicReport) -> str:
    """Render the report as an OKF concept document (``type: Postmortem``).

    Frontmatter + markdown body == an OKF-conformant memory artifact, so the
    forensic record joins the durable knowledge layer instead of being an
    opaque one-off dump.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    desc = f"{report.outcome} on branch {report.work_branch or '(none)'}"
    if report.reason:
        desc += f" -- {report.reason}"
    frontmatter = [
        "---",
        "type: Postmortem",
        f"title: Failed run -- {report.task_id}",
        f"description: {desc}",
        f"timestamp: {now}",
        f"tags: [postmortem, {report.mutation_mode}, {report.outcome}]",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + render(report)


def write_okf_postmortem(report: ForensicReport, repo_dir: str = ".") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{_slug(report.task_id)}-{stamp}.md"
    path = os.path.join(repo_dir, POSTMORTEM_DIR, name)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_okf_postmortem(report))
    return path


def append_log(report: ForensicReport, repo_dir: str = ".") -> str:
    """Append a dated, newest-first entry to the OKF ``log.md`` memory file."""
    path = os.path.join(repo_dir, REPORT_DIR, LOG_NAME)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    reason = report.reason or "no reason recorded"
    entry = (
        f"* **{report.outcome}**: task `{report.task_id}` on "
        f"`{report.work_branch or '(none)'}` -- {reason}"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    except (FileNotFoundError, OSError):
        existing = ""

    lines = existing.splitlines()
    body = [ln for ln in lines if ln.strip() and ln.strip() != LOG_TITLE]
    heading = f"## {date}"
    if body and body[0] == heading:
        rebuilt = [LOG_TITLE, "", heading, entry, *body[1:]]
    else:
        rebuilt = [LOG_TITLE, "", heading, entry, "", *body]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(rebuilt) + "\n")
    return path


def print_badge(outcome: str, path: str) -> None:
    bar = "=" * 64
    print(bar)
    print(f"  [HARNESS] AGENT RUN {outcome.upper()} -- workspace safely contained")
    print(f"  Forensic report: {path}")
    print(bar)
