#!/usr/bin/env python3
"""Escape-hatch interception for the agent's runtime command.

A rogue agent can try to defeat the git latchway by appending ``--no-verify`` or
``-n`` to a ``git commit``/``git push`` it spawns inside ``AGENT_LLM_CMD``. This
module scans the command string, strips those bypass flags *only* where they
apply to a git commit/push segment, and reports what it removed so the caller can
apply a penalty. Commands that do not tamper are returned untouched, byte for
byte, so normal behaviour (and shell quoting) is preserved.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

_BYPASS_FLAGS = frozenset({"--no-verify", "-n"})
_SEPARATORS = frozenset({"&&", "||", "|", ";", "&"})
_GIT_SUBCOMMANDS = frozenset({"commit", "push"})
_GIT_NAMES = frozenset({"git", "git.exe"})


@dataclass
class GuardResult:
    original: str
    sanitized: str
    stripped: list[str] = field(default_factory=list)

    @property
    def tampered(self) -> bool:
        return bool(self.stripped)


def _tokenize(cmd: str) -> list[str] | None:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return None


def sanitize_command(cmd: str | None) -> GuardResult:
    """Strip git bypass flags from ``cmd``; report any that were removed."""
    if not cmd:
        return GuardResult(original=cmd or "", sanitized=cmd or "")

    tokens = _tokenize(cmd)
    if tokens is None:
        # Unparseable (e.g. unbalanced quotes): fall back to a literal scan so a
        # naked bypass flag is still caught rather than silently passing.
        literal = sorted({flag for flag in _BYPASS_FLAGS if f" {flag}" in f" {cmd}"})
        return GuardResult(original=cmd, sanitized=cmd, stripped=literal)

    out: list[str] = []
    stripped: list[str] = []
    in_git = False
    git_sub = ""

    for tok in tokens:
        if tok in _SEPARATORS:
            in_git = False
            git_sub = ""
            out.append(tok)
            continue
        base = os.path.basename(tok).lower()
        if not in_git and base in _GIT_NAMES:
            in_git = True
            git_sub = ""
            out.append(tok)
            continue
        if in_git and not git_sub and not tok.startswith("-"):
            git_sub = tok.lower()
            out.append(tok)
            continue
        if in_git and git_sub in _GIT_SUBCOMMANDS and tok in _BYPASS_FLAGS:
            stripped.append(tok)
            continue
        out.append(tok)

    if not stripped:
        return GuardResult(original=cmd, sanitized=cmd)

    sanitized = " ".join(tok if tok in _SEPARATORS else shlex.quote(tok) for tok in out)
    return GuardResult(original=cmd, sanitized=sanitized, stripped=stripped)
