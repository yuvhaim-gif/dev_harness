"""LLM subprocess seam: environment hardening, invocation, and timeouts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

import command_guard
import telemetry
from lock_policy import compute_allowlist, env_flag
from runner_core import RunContext, log
from telemetry import _env_float


def _harden_git_env(env: dict[str, str]) -> None:
    """Pin git config for the seam so the inherited environment cannot weaken it.

    This is defence-in-depth, not a sandbox: a command-line ``-c
    core.hooksPath=...`` still wins over configuration, which is exactly why the
    command guard flags it and the post-hoc containment gate + CI re-check are
    the authoritative boundaries. True isolation (no network, read-only ``.git``)
    requires running the seam in a container; see the README threat model.
    """
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_PARAMETERS"):
        env.pop(key, None)
    # Drop the env-var form of ``git -c`` (GIT_CONFIG_COUNT + GIT_CONFIG_KEY_*/
    # GIT_CONFIG_VALUE_*): an inherited core.hooksPath override injected this way
    # never appears in the command string the guard scans, so strip it here.
    env.pop("GIT_CONFIG_COUNT", None)
    for key in [k for k in env if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))]:
        env.pop(key, None)


def _seam_base_env() -> dict[str, str]:
    """Base environment for the LLM subprocess.

    Default (no AGENT_ENV_ALLOWLIST) is a full copy, preserving setups that rely
    on inherited vars. When AGENT_ENV_ALLOWLIST is set (comma/newline-separated
    var names), start from ONLY those explicitly named vars -- no AGENT_*/GIT_*
    prefix carve-out, so a secret like AGENT_AWS_SECRET_KEY is not leaked past the
    allowlist. The harness re-injects the AGENT_* task context it needs in
    _llm_env, and _harden_git_env pins git config there, so neither family needs a
    blanket exemption; an operator who genuinely needs a var (e.g. GIT_ASKPASS)
    lists it in the allowlist.

    SKIP_AGENT_HARNESS is always dropped, even in full-copy mode: it is a
    human-only override that disables the local lock/contract hooks, and must
    never be inherited by git commands the agent itself spawns.
    """
    raw = os.getenv("AGENT_ENV_ALLOWLIST")
    if not raw:
        if env_flag("AGENT_ENV_STRICT"):
            raise SystemExit(
                "AGENT_ENV_STRICT=1 but AGENT_ENV_ALLOWLIST is unset: refusing "
                "to copy the full parent environment into the LLM subprocess."
            )
        env = os.environ.copy()
    else:
        names = {n.strip() for n in raw.replace(",", "\n").splitlines() if n.strip()}
        env = {k: v for k, v in os.environ.items() if k in names}
    env.pop("SKIP_AGENT_HARNESS", None)
    return env


def _llm_env(
    ctx: RunContext, phase: str, repair_log: str = "", prompt_file: str = ""
) -> dict[str, str]:
    allow = sorted(compute_allowlist(ctx.task.raw))
    env = _seam_base_env()
    _harden_git_env(env)
    env.update(
        {
            "AGENT_TASK_ID": ctx.task.task_id,
            "AGENT_TASK_DESCRIPTION": ctx.task.description,
            "AGENT_MUTATION_MODE": ctx.task.mutation_mode,
            "AGENT_PHASE": phase,
            "AGENT_ALLOWLIST": "\n".join(allow),
            "AGENT_SPEC_DOCS": "\n".join(ctx.task.spec_docs),
            "AGENT_TESTS": "\n".join(ctx.task.tests),
            "AGENT_TARGETS": "\n".join(ctx.task.targets),
            "AGENT_CONTRACTS": "\n".join(ctx.task.contracts),
            "AGENT_CONTRACT_TESTS": "\n".join(ctx.task.contract_tests),
            "AGENT_HANDOVER_FILE": ctx.handover_path,
            "AGENT_REPAIR_LOG": repair_log,
            "AGENT_REPAIR_PROMPT_FILE": prompt_file,
            "AGENT_TOKEN_USAGE_FILE": telemetry.usage_file_path(),
        }
    )
    return env


def _run_llm(ctx: RunContext, phase: str, repair_log: str = "", prompt_file: str = "") -> bool:
    """Invoke the configured LLM command for ``phase``.

    Provider-agnostic: ``AGENT_LLM_CMD`` is any shell command. Before running, the
    command string is scanned for git bypass flags (``--no-verify``/``-n``); any
    found are stripped and a penalty is logged. Task context, the allowlist, the
    cache-ordered repair prompt, and the token-usage sink are passed through the
    environment. After the command, the per-step token/cost payload is read and
    accumulated. Returns True when a command ran, False when the seam is
    unconfigured (leaving the prior no-op behaviour intact for dry runs/tests).
    """
    cmd = os.getenv("AGENT_LLM_CMD")
    if not cmd:
        log(f"[{phase}] no AGENT_LLM_CMD set; LLM seam is a no-op.")
        return False

    if not os.getenv("AGENT_ENV_ALLOWLIST") and not ctx.env_warned:
        warning = (
            "AGENT_ENV_ALLOWLIST not set -- the LLM subprocess inherits the FULL "
            "parent environment, including any secrets. Set AGENT_ENV_ALLOWLIST to scope it."
        )
        log(f"[{phase}] WARNING: {warning}")
        ctx.git_warnings.append(warning)
        ctx.env_warned = True

    guard = command_guard.sanitize_command(cmd)
    if guard.tampered:
        warning = (
            f"escape-hatch attempt in AGENT_LLM_CMD: stripped {guard.stripped} "
            f"(git bypass flags). Charging the guard-penalty counter."
        )
        log(f"[{phase}] PENALTY: {warning}")
        ctx.git_warnings.append(warning)
        ctx.guard_penalties += 1
    if guard.suspicious:
        warning = (
            f"hook-evasion pattern in AGENT_LLM_CMD: {guard.flagged}. These cannot "
            "be stripped from an arbitrary shell command; the post-hoc containment "
            "gate and CI re-check are the backstop. Charging the guard-penalty counter."
        )
        log(f"[{phase}] PENALTY: {warning}")
        ctx.git_warnings.append(warning)
        ctx.guard_penalties += 1
        ctx.guard_flagged += 1
    run_cmd = guard.sanitized

    usage_path = telemetry.usage_file_path()
    os.makedirs(os.path.dirname(usage_path) or ".", exist_ok=True)
    telemetry.clear_usage_file(usage_path)

    log(f"[{phase}] invoking AGENT_LLM_CMD (provider-agnostic seam).")
    step_timeout = _env_float("AGENT_STEP_TIMEOUT_SECONDS")
    # Put the seam shell in its own session / process group so a timeout can
    # kill the WHOLE tree. With shell=True, subprocess.run's own timeout only
    # SIGKILLs the immediate child (the shell); a forked grandchild
    # (sh -> bash -> sleep ...) would be orphaned and keep mutating the tree
    # after we have already rolled back. The tree is killed via killpg (POSIX)
    # / taskkill /T (Windows), which closes that hole on both platforms.
    argv_json = os.getenv("AGENT_LLM_ARGV")
    if argv_json:
        run_target: Any = json.loads(argv_json)
        shell = False
    else:
        run_target = run_cmd
        shell = True

    popen_kwargs: dict[str, Any] = {
        "shell": shell,
        "env": _llm_env(ctx, phase, repair_log, prompt_file),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(run_target, **popen_kwargs)
    try:
        proc.communicate(timeout=step_timeout)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            # taskkill /T tears down the whole child tree (CREATE_NEW_PROCESS_GROUP
            # gives us a clean group to target); proc.kill() is the fallback.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            except OSError:
                pass
            proc.kill()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
        proc.communicate()
        ctx.timed_out = "step timeout (AGENT_STEP_TIMEOUT_SECONDS)"
        log(
            f"[{phase}] WARNING: AGENT_LLM_CMD exceeded AGENT_STEP_TIMEOUT_SECONDS; "
            "treating as a failed step."
        )
        return True
    if proc.returncode != 0:
        log(f"[{phase}] WARNING: AGENT_LLM_CMD exited {proc.returncode}.")

    # Wall-clock ceiling spanning every step of the run, not just this one.
    max_run = _env_float("MAX_RUN_SECONDS")
    if max_run is not None and (time.monotonic() - ctx.start_time) > max_run:
        ctx.timed_out = "wall-clock timeout (MAX_RUN_SECONDS)"

    step = ctx.ledger.record_from_file(phase, usage_path)
    if step is not None:
        log(f"[{phase}] telemetry: {ctx.ledger.summary()}")
    return True
