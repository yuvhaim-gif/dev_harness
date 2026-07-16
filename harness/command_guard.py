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
the post-hoc containment gate (see ``runner_containment._containment_breach``) plus the
server-side CI re-check (see ``harness/ci_enforce.py``) are the authoritative
defences.

The same evasion applies to the bypass *flags*: the structured pass only strips
``--no-verify``/``-n`` when ``git`` is a clean shell token, so hiding git behind
command substitution (``$(git commit --no-verify)``), backticks
(```git commit --no-verify```), or a shell variable (``$GIT commit --no-verify``)
slips a bypass flag past the stripper untouched. Those obfuscated tokens cannot
be safely rewritten either, so like the plumbing patterns above they are
*flagged* (and charged a guard penalty) rather than silently passed.

The mirror of that hole is an obfuscated bypass *flag* on an otherwise clean
git: wrapping the flag in a command substitution
(``git commit -m x $(echo --no-verify)`` -> the argument ``--no-verify)``) or
routing it through a shell variable
(``FLAG=--no-verify; git commit -m x $FLAG``) both keep ``git`` a clean token
while the bypass never appears as the bare ``--no-verify`` argument the strip
removes. Neither can be safely rewritten in place, so they are *flagged*
(``obfuscated git-bypass`` for the wrapped-argument form, ``indirected
git-bypass`` for the variable form) and charged a guard penalty.

A git bypass can also be buried inside a shell interpreter's script argument
(``sh -c "git commit --no-verify"``, ``bash -lc ...``, ``cmd /c ...``): the
quoted script is a single opaque token to the outer parse, so the structured
strip never sees the inner ``git``. The script argument cannot be safely
rewritten in place, so its contents are scanned recursively and any bypass flag,
plumbing subcommand, or hooks-path override found inside is *flagged* (and
charged a penalty) rather than passed.
"""

from __future__ import annotations

import os
import re
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

# Shell interpreters whose ``-c``/``/c`` argument is an opaque script string the
# outer parse cannot see into; its contents are scanned recursively.
_SHELL_INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "dash", "ash", "ksh", "cmd", "cmd.exe", "powershell", "pwsh"}
)
# The "run this string" flag: POSIX ``-c`` (possibly combined, e.g. ``-lc``) or
# the Windows ``/c``.
_RUN_STRING_FLAG = re.compile(r"^(?:-[a-z]*c|/c)$", re.IGNORECASE)

# Non-shell interpreters that can equally spawn git from an inline script; their
# eval flag is ``-c`` (python) or ``-e``/``-E``/``--eval`` (perl/ruby/node).
_CODE_INTERPRETERS = frozenset(
    {"python", "python2", "python3", "py", "perl", "ruby", "node", "deno"}
)
_EVAL_FLAG = re.compile(r"^(?:-[a-z]*c|-[eE]|--eval|--command)$")

# git commit/push boolean short flags that may stack ahead of ``-n`` inside one
# combined token (``-an`` == ``-a -n``). Any other char takes an argument and
# ends the boolean run, so its ``n`` is a value, not no-verify (``-mn`` == ``-m n``).
_STACKABLE_SHORT_BOOLS = frozenset("nasevqp")

# git's dashed builtins: the git name and its subcommand fused into one token.
_GIT_DASHED = frozenset({"git-commit", "git-commit.exe", "git-push", "git-push.exe"})

# A shell variable assignment (``NAME=value``) and a ``$NAME`` / ``${NAME}``
# expansion, used to trace a bypass flag routed through a shell variable.
_ASSIGN_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.DOTALL)
_VAR_REF_RE = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")


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
    """Flag a bypass flag reaching a git commit/push through shell-meta wrapping.

    Two mirror-image evasions are caught, both scoped per shell segment and both
    requiring a ``commit``/``push`` subcommand so a benign
    ``echo -n hi && git commit`` (bypass in a different segment) never fires:

    * an **obfuscated git** token (``$(git``, ```git``, ``$GIT``) carrying any
      bypass flag — the structured strip skips the wrapped git entirely; and
    * an **obfuscated bypass flag** (``$(echo --no-verify)`` -> the argument
      ``--no-verify)``) riding on an otherwise *clean* ``git commit`` — the flag
      survives because it is not the bare ``--no-verify`` token the strip drops.

    A clean flag on a clean git (``git commit --no-verify``) is left to the
    structured strip and is deliberately *not* re-flagged here.
    """
    found: set[str] = set()
    obf_git = sub = False
    clean_bypass: set[str] = set()
    obf_bypass: set[str] = set()

    def _flush() -> None:
        if not sub:
            return
        found.update(obf_bypass)
        if obf_git:
            found.update(clean_bypass)

    for tok in tokens:
        if tok in _SEPARATORS:
            _flush()
            obf_git = sub = False
            clean_bypass = set()
            obf_bypass = set()
            continue
        if _is_obfuscated_git(tok):
            obf_git = True
        if tok.lower() in _GIT_SUBCOMMANDS:
            sub = True
        core = tok.strip(_SHELL_META)
        if core in _BYPASS_FLAGS:
            (clean_bypass if core == tok else obf_bypass).add(core)
    _flush()
    return sorted(found)


def _bypass_vars(tokens: list[str]) -> dict[str, list[str]]:
    """Map each shell variable assigned a bypass flag value to those flags.

    ``FLAG=--no-verify`` (optionally with a tokenizer-glued trailing separator,
    ``FLAG=--no-verify;``) records ``FLAG -> ['--no-verify']``.
    """
    found: dict[str, list[str]] = {}
    for tok in tokens:
        match = _ASSIGN_RE.match(tok)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        pieces = set(re.split(r"[\s;&|]+", value))
        hit = sorted(flag for flag in _BYPASS_FLAGS if flag in pieces)
        if hit:
            found[name] = hit
    return found


def _indirect_bypass(tokens: list[str]) -> list[str]:
    """Flag a bypass flag smuggled to git commit/push through a shell variable.

    ``FLAG=--no-verify; git commit -m x $FLAG`` assigns the flag to a variable
    and expands it beside the commit, so neither the structured strip nor the
    obfuscated-token scan (the literal ``--no-verify`` never appears as a git
    argument) sees it. Collect any variable assigned a bypass flag value, then
    flag it when a ``$VAR`` / ``${VAR}`` expansion of that variable lands in a
    ``commit``/``push`` segment. A benign ``git commit -m "$MSG"`` is untouched
    because ``MSG`` was never assigned a bypass flag.
    """
    bypass_vars = _bypass_vars(tokens)
    if not bypass_vars:
        return []
    found: set[str] = set()
    sub = False
    hits: set[str] = set()

    def _flush() -> None:
        if sub and hits:
            found.update(hits)

    for tok in tokens:
        if tok in _SEPARATORS:
            _flush()
            sub = False
            hits = set()
            continue
        if tok.lower() in _GIT_SUBCOMMANDS:
            sub = True
        for name in _VAR_REF_RE.findall(tok):
            if name in bypass_vars:
                hits.update(bypass_vars[name])
    _flush()
    return sorted(found)


# Cap how deep the interpreter-in-interpreter recursion may go so a crafted
# ``sh -c "sh -c \"sh -c ...\""`` nest cannot exhaust the stack. Well past any
# legitimate nesting; beyond it the non-recursive linear strip still runs.
_MAX_SCAN_DEPTH = 8


def _shell_c_evasion(tokens: list[str], depth: int) -> list[str]:
    """Flag a git bypass buried inside a shell interpreter's ``-c`` script.

    For each ``<interpreter> [flags] -c <script>`` run, recursively sanitise the
    opaque ``<script>`` token; anything the inner pass would have stripped or
    flagged is surfaced here (it cannot be rewritten in place from out here, so
    it is flagged and penalised rather than passed).
    """
    found: list[str] = []
    for i, tok in enumerate(tokens):
        if os.path.basename(tok).lower() not in _SHELL_INTERPRETERS:
            continue
        j = i + 1
        while j < len(tokens):
            nxt = tokens[j]
            if _RUN_STRING_FLAG.match(nxt):
                if j + 1 < len(tokens):
                    inner = sanitize_command(tokens[j + 1], _depth=depth + 1)
                    found += [f"shell -c git-bypass: {f}" for f in inner.stripped]
                    found += [f"shell -c {f}" for f in inner.flagged]
                break
            if nxt.startswith(("-", "/")):
                j += 1
                continue
            break
    return sorted(set(found))


def _strip_short_no_verify(tok: str) -> tuple[str | None, bool]:
    """Remove a stacked boolean ``-n`` (no-verify) from a combined short token.

    Returns ``(rewritten, stripped)``. ``rewritten`` is the token minus the
    ``n`` (``-nm`` -> ``-m``), or ``None`` when only ``-n`` remained. A token
    whose ``n`` is really an argument value (``-mn`` == ``-m n``), or that does
    not combine, is returned untouched with ``stripped=False``.
    """
    if len(tok) < 2 or not tok.startswith("-") or tok.startswith("--"):
        return tok, False
    kept: list[str] = []
    stripped = False
    for idx, ch in enumerate(tok[1:]):
        if ch == "n" and not stripped:
            stripped = True
            continue
        if ch not in _STACKABLE_SHORT_BOOLS:
            # An argument-taking short flag; it and the rest are its value.
            kept.append(tok[1 + idx :])
            break
        kept.append(ch)
    if not stripped:
        return tok, False
    rest = "".join(kept)
    return (f"-{rest}" if rest else None), True


def _scan_inline_script(script: str, depth: int) -> list[str]:
    """Surface a git bypass hidden in a code interpreter's inline script.

    The script is opaque to the outer parse and is often *not* shell (e.g.
    Python), so besides the recursive shell scan it is split on code punctuation
    to catch a bypass flag quoted as a string literal
    (``subprocess.run(["git", "commit", "--no-verify"])``).
    """
    found: list[str] = []
    inner = sanitize_command(script, _depth=depth + 1)
    found += [f"interpreter git-bypass: {f}" for f in inner.stripped]
    found += [f"interpreter {f}" for f in inner.flagged]
    pieces = set(re.split(r"[\s'\"(),\[\]]+", script))
    for flag in sorted(_BYPASS_FLAGS):
        if flag in pieces:
            found.append(f"interpreter git-bypass: {flag}")
    found += [f"interpreter {f}" for f in _literal_flagged(script)]
    return found


def _code_interpreter_evasion(tokens: list[str], depth: int) -> list[str]:
    """Flag a git bypass buried in ``python -c`` / ``perl -e`` / ``node -e`` ...

    Mirrors ``_shell_c_evasion`` for non-shell interpreters, whose inline-eval
    flag is ``-c`` or ``-e``/``-E``/``--eval`` rather than only ``-c``/``/c``.
    """
    found: list[str] = []
    for i, tok in enumerate(tokens):
        if os.path.basename(tok).lower() not in _CODE_INTERPRETERS:
            continue
        j = i + 1
        while j < len(tokens):
            nxt = tokens[j]
            if _EVAL_FLAG.match(nxt):
                if j + 1 < len(tokens):
                    found += _scan_inline_script(tokens[j + 1], depth)
                break
            if nxt.startswith(("-", "/")):
                j += 1
                continue
            break
    return sorted(set(found))


def sanitize_command(cmd: str | None, *, _depth: int = 0) -> GuardResult:
    """Strip git bypass flags from ``cmd`` and flag unstrippable evasion.

    ``_depth`` is internal: it tracks interpreter-in-interpreter recursion so a
    pathologically nested command cannot exhaust the stack (see
    ``_MAX_SCAN_DEPTH``).
    """
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
    pending_config = False  # ...and that flag was -c/--config-env (alias probe)

    for tok in tokens:
        if _HOOKSPATH_NEEDLE in tok.lower():
            flagged.append("core.hooksPath override")
        if tok in _SEPARATORS:
            in_git = False
            git_sub = ""
            skip_value = False
            pending_config = False
            out.append(tok)
            continue
        base = os.path.basename(tok).lower()
        if not in_git and base in _GIT_NAMES:
            in_git = True
            git_sub = ""
            skip_value = False
            pending_config = False
            out.append(tok)
            continue
        if not in_git and base in _GIT_DASHED:
            # ``git-commit``/``git-push`` fuse the git name and its subcommand
            # into one token; treat them as an in-git commit/push segment so the
            # bypass strip below still applies.
            in_git = True
            git_sub = base.split(".", 1)[0].split("-", 1)[1]
            skip_value = False
            pending_config = False
            out.append(tok)
            continue
        if in_git and not git_sub:
            # Still scanning git's global options, before the subcommand.
            if skip_value:
                skip_value = False
                if pending_config and tok.lower().startswith("alias."):
                    # ``git -c alias.x=commit x`` smuggles a commit/push through
                    # an alias the structured strip cannot follow; flag it.
                    flagged.append("git alias override")
                pending_config = False
                out.append(tok)
                continue
            if tok in _GIT_VALUE_FLAGS:
                skip_value = True
                pending_config = tok in {"-c", "--config-env"}
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
        if in_git and git_sub in _GIT_SUBCOMMANDS:
            if tok == "--no-verify":
                stripped.append(tok)
                continue
            rewritten, was_stripped = _strip_short_no_verify(tok)
            if was_stripped:
                # Report the logical flag; the rewritten token keeps the rest of
                # a combined cluster (``-nm`` -> stripped ``-n``, kept ``-m``).
                stripped.append("-n")
                if rewritten is not None:
                    out.append(rewritten)
                continue
        out.append(tok)

    flagged.extend(f"obfuscated git-bypass: {f}" for f in _obfuscated_bypass(tokens))
    flagged.extend(f"indirected git-bypass: {f}" for f in _indirect_bypass(tokens))
    if _depth < _MAX_SCAN_DEPTH:
        flagged.extend(_shell_c_evasion(tokens, _depth))
        flagged.extend(_code_interpreter_evasion(tokens, _depth))
    flagged = sorted(set(flagged))
    if not stripped:
        return GuardResult(original=cmd, sanitized=cmd, flagged=flagged)

    sanitized = " ".join(tok if tok in _SEPARATORS else shlex.quote(tok) for tok in out)
    return GuardResult(original=cmd, sanitized=sanitized, stripped=stripped, flagged=flagged)
