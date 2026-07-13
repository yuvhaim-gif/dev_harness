#!/usr/bin/env python3
"""Condense raw tool output into a lean, token-efficient repair context.

Feeding a 10,000-line pre-commit/pytest dump back into an LLM during the
autorepair state is slow and expensive. This module parses the dump with
structural regex anchors, keeps only the load-bearing signal -- failing
assertions, type/lint errors, and the exact ``file:line`` references -- and
attaches a 3-line source window around each referenced location. The result is
bounded, ordered, and deterministic so it also plays well with prompt caching.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_MYPY = re.compile(
    r"^(?P<file>[^\s:][^:\n]*\.py):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<level>error|warning|note):\s*(?P<msg>.+)$"
)
_RUFF = re.compile(
    r"^(?P<file>[^\s:][^:\n]*\.py):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<code>[A-Z]+\d+)\s+(?P<msg>.+)$"
)
_OKF = re.compile(r"^\s*-?\s*(?P<file>[^\s:][^:\n]*\.md):\s*(?P<msg>.+)$")
_PYTEST_E = re.compile(r"^E\s{2,}(?P<msg>.+)$")
_FAILED = re.compile(r"^(?P<kind>FAILED|ERROR)\s+(?P<msg>.+)$")
_LOC = re.compile(r"(?P<file>[\w./\\-]+\.py):(?P<line>\d+)")

_NOISE_SUBSTRINGS = (
    "site-packages",
    "requirement already satisfied",
    "pip install",
    "warnings summary",
    "deprecationwarning",
    "-- docs:",
    "passed,",
    "no tests ran",
    "collecting ...",
    "platform ",
    "rootdir:",
    "plugins:",
    "cachedir:",
)

_MAX_FAILURES = 12
_MAX_ERRORS = 12
_MAX_CONTEXTS = 6
_DEFAULT_MAX_CHARS = 1600


@dataclass
class _Signals:
    failures: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    locations: list[tuple[str, int]] = field(default_factory=list)


def _is_noise(line: str) -> bool:
    low = line.lower()
    return any(token in low for token in _NOISE_SUBSTRINGS)


def _push_unique(items: list[str], value: str, cap: int) -> None:
    if value and value not in items and len(items) < cap:
        items.append(value)


def _push_location(locations: list[tuple[str, int]], file: str, line: int) -> None:
    norm = file.replace("\\", "/")
    pair = (norm, line)
    if pair not in locations and len(locations) < _MAX_CONTEXTS * 3:
        locations.append(pair)


def _extract_signals(lines: list[str]) -> _Signals:
    sig = _Signals()
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        mypy = _MYPY.match(line)
        if mypy and mypy.group("level") != "note":
            _push_unique(
                sig.errors,
                f"{mypy.group('file')}:{mypy.group('line')}: {mypy.group('msg')}",
                _MAX_ERRORS,
            )
            _push_location(sig.locations, mypy.group("file"), int(mypy.group("line")))
            continue

        ruff = _RUFF.match(line)
        if ruff:
            _push_unique(
                sig.errors,
                f"{ruff.group('file')}:{ruff.group('line')}: "
                f"{ruff.group('code')} {ruff.group('msg')}",
                _MAX_ERRORS,
            )
            _push_location(sig.locations, ruff.group("file"), int(ruff.group("line")))
            continue

        okf_err = _OKF.match(line)
        if okf_err:
            _push_unique(
                sig.errors,
                f"{okf_err.group('file')}: {okf_err.group('msg').strip()}",
                _MAX_ERRORS,
            )
            continue

        pe = _PYTEST_E.match(line)
        if pe:
            _push_unique(sig.failures, pe.group("msg").strip(), _MAX_FAILURES)
            continue

        failed = _FAILED.match(line)
        if failed:
            _push_unique(
                sig.failures,
                f"{failed.group('kind')} {failed.group('msg').strip()}",
                _MAX_FAILURES,
            )

        if _is_noise(line):
            continue
        loc = _LOC.search(line)
        if loc:
            _push_location(sig.locations, loc.group("file"), int(loc.group("line")))
    return sig


def _source_context(repo_dir: str, file: str, line: int) -> str | None:
    root = os.path.realpath(repo_dir)
    candidate = os.path.realpath(os.path.join(root, file))
    try:
        inside = os.path.commonpath([root, candidate]) == root
    except ValueError:
        inside = False
    if os.path.isabs(file) or not inside:
        return None
    try:
        with open(candidate, encoding="utf-8") as fh:
            src = fh.read().splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    if line < 1 or line > len(src):
        return None
    start = max(1, line - 1)
    end = min(len(src), line + 1)
    rows = []
    for num in range(start, end + 1):
        marker = ">>" if num == line else "  "
        rows.append(f"  {marker} {num}: {src[num - 1]}")
    return f"{file}:{line}\n" + "\n".join(rows)


def condense(
    raw: str,
    repo_dir: str = ".",
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Return a compact summary of ``raw`` tool output (empty stays empty)."""
    if not raw or not raw.strip():
        return ""

    lines = raw.splitlines()
    sig = _extract_signals(lines)

    contexts: list[str] = []
    for file, line in sig.locations:
        if len(contexts) >= _MAX_CONTEXTS:
            break
        ctx = _source_context(repo_dir, file, line)
        if ctx:
            contexts.append(ctx)

    parts: list[str] = []
    if sig.failures:
        parts.append("FAILED ASSERTIONS:\n" + "\n".join(f"  - {f}" for f in sig.failures))
    if sig.errors:
        parts.append("TYPE/LINT ERRORS:\n" + "\n".join(f"  - {e}" for e in sig.errors))
    if contexts:
        parts.append("SOURCE CONTEXT:\n" + "\n\n".join(contexts))

    if not parts:
        tail = [ln for ln in lines if ln.strip()][-15:]
        parts.append("LOG TAIL:\n" + "\n".join(tail))

    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text
