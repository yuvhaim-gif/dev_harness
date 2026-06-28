#!/usr/bin/env python3
"""Escape-hatch interception for the agent's runtime command.

A rogue agent can try to defeat the git latchway by appending ``--no-verify`` or
``-n`` to a ``git commit``/``git push`` it spawns inside ``AGENT_LLM_CMD``. This
module scans the command string, strips those bypass flags *only* where they
apply to a git commit/push segment, and reports what it removed so the caller can
apply a penalty. Commands that do not tamper are returned untouched, byte for
byte, so normal behaviour (and shell quoting) is preserved.

Stripping two literal flags only raises the cost of an *accidental* bypass; it
is not a security boundary. A determined agent can still skip the hooks with
``git -c core.hooksPath=...`` or by writing a commit through low-level plumbing
(``commit-tree``/``update-ref``). Those patterns cannot be safely rewritten out
of an arbitrary shell string, so instead of stripping them this module *flags*
them: the orchestrator logs the policy event, penalises the repair counter, and
the post-hoc containment gate (see ``agent_runner._containment_breach``) plus the
server-side CI re-check (see ``harness/ci_enforce.py``) are the authoritative
defences.

The same evasion applies to the bypass *flags*: the structured pass only strips
``--no-verify``/``-n`` when ``git`` is a clean shell token, so hiding git behind
command substitution (``$(git commit --no-verify)``), backticks
(```git commit --no-verify```), or a shell variable (``$GIT commit --no-verify``)
slips a bypass flag past the stripper untouched. Those obfuscated tokens cannot
be safely rewritten either, so like the plumbing patterns above they are
*flagged* (and charged a guard penalty) rather than silently passed.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

_BYPASS_FLAGS = frozenset({"--no-verify", "-n"})
_SEPARATORS = frozenset({"&&", "||", "|", ";", "&"})
_GIT_SUBCOMMANDS = frozenset({"commit", "push"})
_GIT_NAMES = frozenset({"git", "git.exe"})

# Global git options that consume the *next* token as their value, so that
# value must never be mistaken for the subcommand (e.g. ``git -C <path> commit``).
_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)

# Low-level git that writes history without firing the pre-commit hooks.
_FLAGGED_SUBCOMMANDS = frozenset(
    {"commit-tree", "update-ref", "fast-import", "hash-object", "update-index"}
)
_HOOKSPATH_NEEDLE = "hookspath"

# Shell-meta characters wrapping a token when git (or a bypass flag) is hidden in
# a command substitution / backtick / variable expansion, e.g. ``$(git``,
# ```git``, ``$GIT``, ``${GIT}`` or ``--no-verify)``.
_SHELL_META = "$(){}`\"'"


@dataclass
class GuardResult:
    original: str
    sanitized: str
    stripped: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)

    @property
    def tampered(self) -> bool:
        return bool(self.stripped)

    @property
    def suspicious(self) -> bool:
        """True when an unstrippable hook-evasion pattern was detected."""
        return bool(self.flagged)


def _tokenize(cmd: str) -> list[str] | None:
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return None


def _literal_flagged(cmd: str) -> list[str]:
    low = cmd.lower()
    flagged: list[str] = []
    if _HOOKSPATH_NEEDLE in low:
        flagged.append("core.hooksPath override")
    for sub in sorted(_FLAGGED_SUBCOMMANDS):
        if f" {sub}" in f" {low}":
            flagged.append(f"low-level git: {sub}")
    return sorted(set(flagged))


def _is_obfuscated_git(tok: str) -> bool:
    """True when ``tok`` is a git invocation hidden behind shell-meta wrapping.

    A clean ``git`` token is returned untouched by ``strip``; an obfuscated one
    (``$(git``, ```git``, ``$GIT``, ``${GIT}``) loses its wrapper and only then
    resolves to a git name, which is exactly what defeats the structured strip.
    """
    core = tok.strip(_SHELL_META)
    if core == tok:
        return False
    return os.path.basename(core).lower() in _GIT_NAMES


def _obfuscated_bypass(tokens: list[str]) -> list[str]:
    """Flag bypass flags riding on an obfuscated git commit/push.

    Per shell segment, require all three signals together — an obfuscated git
    token, a ``commit``/``push`` subcommand, and a bypass flag — so a benign
    ``echo -n hi && git commit`` (clean git, flag in a different segment) is
    never flagged.
    """
    found: set[str] = set()
    obf_git = sub = False
    bypass: set[str] = set()

    def _flush() -> None:
        if obf_git and sub and bypass:
            found.update(bypass)

    for tok in tokens:
        if tok in _SEPARATORS:
            _flush()
            obf_git = sub = False
            bypass = set()
            continue
        if _is_obfuscated_git(tok):
            obf_git = True
        if tok.lower() in _GIT_SUBCOMMANDS:
            sub = True
        core = tok.strip(_SHELL_META)
        if core in _BYPASS_FLAGS:
            bypass.add(core)
    _flush()
    return sorted(found)


def sanitize_command(cmd: str | None) -> GuardResult:
    """Strip git bypass flags from ``cmd`` and flag unstrippable evasion."""
    if not cmd:
        return GuardResult(original=cmd or "", sanitized=cmd or "")

    tokens = _tokenize(cmd)
    if tokens is None:
        # Unparseable (e.g. unbalanced quotes): fall back to a literal scan so a
        # naked bypass flag or evasion pattern is still caught rather than
        # silently passing.
        literal = sorted({flag for flag in _BYPASS_FLAGS if f" {flag}" in f" {cmd}"})
        return GuardResult(
            original=cmd, sanitized=cmd, stripped=literal, flagged=_literal_flagged(cmd)
        )

    out: list[str] = []
    stripped: list[str] = []
    flagged: list[str] = []
    in_git = False
    git_sub = ""
    skip_value = False  # the previous token was a value-taking global flag

    for tok in tokens:
        if _HOOKSPATH_NEEDLE in tok.lower():
            flagged.append("core.hooksPath override")
        if tok in _SEPARATORS:
            in_git = False
            git_sub = ""
            skip_value = False
            out.append(tok)
            continue
        base = os.path.basename(tok).lower()
        if not in_git and base in _GIT_NAMES:
            in_git = True
            git_sub = ""
            skip_value = False
            out.append(tok)
            continue
        if in_git and not git_sub:
            # Still scanning git's global options, before the subcommand.
            if skip_value:
                skip_value = False
                out.append(tok)
                continue
            if tok in _GIT_VALUE_FLAGS:
                skip_value = True
                out.append(tok)
                continue
            if tok.startswith("-"):
                # A no-value global flag, or a joined ``--flag=value`` form;
                # never the subcommand.
                out.append(tok)
                continue
            git_sub = tok.lower()
            if git_sub in _FLAGGED_SUBCOMMANDS:
                flagged.append(f"low-level git: {git_sub}")
            out.append(tok)
            continue
        if in_git and git_sub in _GIT_SUBCOMMANDS and tok in _BYPASS_FLAGS:
            stripped.append(tok)
            continue
        out.append(tok)

    flagged.extend(f"obfuscated git-bypass: {f}" for f in _obfuscated_bypass(tokens))
    flagged = sorted(set(flagged))
    if not stripped:
        return GuardResult(original=cmd, sanitized=cmd, flagged=flagged)

    sanitized = " ".join(tok if tok in _SEPARATORS else shlex.quote(tok) for tok in out)
    return GuardResult(original=cmd, sanitized=sanitized, stripped=stripped, flagged=flagged)
