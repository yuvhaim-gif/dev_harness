# LLM integration seam

> **Relevant source:** [`../runner_llm.py`](../runner_llm.py),
> [`../telemetry.py`](../telemetry.py),
> [`../log_condenser.py`](../log_condenser.py),
> [`../prompt_builder.py`](../prompt_builder.py),
> [`../command_guard.py`](../command_guard.py),
> [`../lock_policy.py`](../lock_policy.py).

The mutation and autorepair phases dispatch to a single environment variable,
`AGENT_LLM_CMD` — any shell command. The orchestrator exports the full task
context to it so the model can edit only permitted files:

| Variable | Purpose |
|----------|---------|
| `AGENT_TASK_ID`           | Active task id (from `AGENTS.md`). |
| `AGENT_TASK_DESCRIPTION`  | Free-text description from the ledger. |
| `AGENT_MUTATION_MODE`     | `evolve` or `isolated`. |
| `AGENT_PHASE`             | `mutate` or `autorepair`. |
| `AGENT_ALLOWLIST`         | Newline-joined paths the LLM may stage. |
| `AGENT_SPEC_DOCS`         | Newline-joined declared spec docs. |
| `AGENT_TESTS`             | Newline-joined declared tests. |
| `AGENT_TARGETS`           | Newline-joined declared source targets. |
| `AGENT_CONTRACTS`         | Newline-joined contract files. |
| `AGENT_CONTRACT_TESTS`    | Newline-joined contract-binding tests. |
| `AGENT_HANDOVER_FILE`     | Path to the recovered prior-session journal (if any). |
| `AGENT_REPAIR_LOG`        | **Condensed** hook-log excerpt from the failed Enforce attempt (autorepair only). |
| `AGENT_REPAIR_PROMPT_FILE`| Path to the cache-ordered repair prompt written for this attempt (autorepair only). |
| `AGENT_TOKEN_USAGE_FILE`  | Path the command should write its per-step token/cost JSON payload to, for the budget ledger. |

When `AGENT_LLM_CMD` is unset the seam is a no-op and logs that fact —
useful for dry runs and for the framework's own tests. The subprocess
environment is scoped by **`AGENT_ENV_ALLOWLIST`** (comma/newline-separated
names); unset means a full copy of the parent environment plus a one-time
warning, and the resulting `env_scope` (`allowlisted` / `full_copy`) is surfaced
in the forensic report and `--doctor`. When the allowlist is set, **only the
explicitly named vars** pass through — there is no `AGENT_*` / `GIT_*` prefix
carve-out, so a secret like `AGENT_AWS_SECRET_KEY` is not leaked to the seam
(the harness re-injects the `AGENT_*` task context it needs and pins git config
itself); an operator who genuinely needs an inherited var such as `GIT_ASKPASS`
lists it explicitly. Before every invocation the command
string is run through the **escape-hatch guard** (`harness/command_guard.py`):
any `--no-verify` / `-n` appended to a `git commit` / `git push` segment is
stripped, the run continues with the sanitized command, and a **separate
`guard_penalties` counter** takes a penalty — it has its own exit-4 ceiling and
leaves the autorepair budget untouched, so a tamper-once agent keeps its full
repair allowance while a persistent escape attempt is still contained. The strip
also unpacks **combined short flags** (`-nm` == `-n -m`, so the stacked `-n` is
removed while `-m` and its message survive), recognises git's **dashed builtins**
(`git-commit` / `git-push`), and preserves a genuine value that merely looks like
a flag (`-mn` == `-m n`).

The guard is explicitly **not a security boundary**. Stripping two literal flags
only raises the cost of an *accidental* bypass; a determined agent can still skip
the hooks with `git -c core.hooksPath=…` or by writing history through plumbing
(`commit-tree` / `update-ref`). Those patterns cannot be safely rewritten out of
an arbitrary shell string, so the guard *flags* them (`GuardResult.suspicious`)
and the run is penalised — but the **authoritative** defences are the post-hoc
containment gate and the server-side CI re-check (see
[Containment defences](containment-and-diagnostics.md#containment-defences)), both of
which inspect committed history and therefore hold regardless of which git the
agent ran.

A git bypass can also be buried inside a **shell interpreter's script argument**
(`sh -c "git commit --no-verify"`, `bash -lc …`, `cmd /c …`): the quoted script
is a single opaque token to the outer parse, so the structured strip never sees
the inner `git`. The guard recursively sanitises that `-c` / `/c` script and
*flags* any bypass flag, plumbing subcommand, or hooks-path override it finds
inside (it cannot be rewritten in place from outside the quotes), charging the
same `guard_penalties` hit — closing a hole where such a wrapped command
previously passed through completely undetected. The same treatment extends to
**non-shell interpreters** (`python -c …`, `perl -e …`, `ruby`, `node`, `deno`):
their inline-eval string is scanned for a git bypass even when it is quoted code
rather than a shell command. Finally, an **alias indirection**
(`git -c alias.x=commit x …`) that smuggles a commit/push past the structured
strip is likewise flagged.

## Token & cost budgeting

`harness/telemetry.py` keeps a running `TokenLedger`. After each LLM step
the runner reads the JSON payload the command wrote to `AGENT_TOKEN_USAGE_FILE`,
normalises the many provider field spellings (`input_tokens` / `prompt_tokens`,
`output_tokens` / `completion_tokens`, nested `usage` / `tool_token_usage`, …)
into one shape, and accumulates input/output/total tokens and USD cost. When a
payload omits an explicit cost it is derived from per-1K pricing. If any
configured ceiling is breached the run performs an **immediate financial abort**
— forensic report, hard `git reset --hard` rollback, journal `escalated`, and
**exit 3**.

| Variable | Purpose |
|----------|---------|
| `MAX_TOTAL_TOKENS`        | Hard ceiling on cumulative total tokens. |
| `MAX_RUN_COST_USD`        | Hard ceiling on cumulative USD cost. |
| `AGENT_COST_PER_1K_INPUT` | Per-1K input-token price used to derive cost when a payload omits it. |
| `AGENT_COST_PER_1K_OUTPUT`| Per-1K output-token price used to derive cost when a payload omits it. |
| `AGENT_TOKEN_USAGE_FILE`  | Override the default `.harness/telemetry/usage.json` sink path. |
| `AGENT_STEP_TIMEOUT_SECONDS` | Per-step ceiling on a single `AGENT_LLM_CMD` invocation; an overrun aborts with a *timeout* reason and **exit 3**. The seam runs in its own session / process group, so an overrun kills the **whole process tree** (not just the immediate shell) — a forked grandchild cannot survive the timeout and keep mutating the tree after the rollback. |
| `MAX_RUN_SECONDS`         | Wall-clock ceiling on the whole run; an overrun aborts with a *timeout* reason and **exit 3**. |

The same circuit-breaker also enforces the two **time** ceilings above; a
timeout shares the financial abort's exit code (3) but stamps a distinct
*timeout* reason in the forensic report. Everything degrades gracefully: a
missing file, malformed JSON, or unset budgets/timeouts yields zero usage and no
abort, so dry runs and tests stay green.

## Context truncation & error condensation

`harness/log_condenser.py` never feeds a raw multi-thousand-line dump back
to the model. It parses tool output with structural regex anchors (mypy, ruff,
pytest `E   ` assertions, `FAILED` / `ERROR` lines), strips package-manager and
summary noise, keeps the exact `file:line` references with a **3-line source
window** around each, and bounds the result. The output is ordered and
deterministic, which also makes it cache-friendly.

## Deterministic prompt caching

`harness/prompt_builder.py` assembles each repair prompt strictly
most-static → most-dynamic so provider prompt caches reuse the unchanging head
across recursive repair cycles:

1. **Static** — immutable framework rules (edit only the allowlist, never the
   always-locked files, no bypass flags, smallest change, keep every `spec_doc`'s
   OKF frontmatter with a non-empty `type` and no contract `timestamp`, …).
2. **Semi-static** — the task schema, allowlist arrays, and `AGENTS.md`
   boundaries.
3. **Dynamic** — the current working diff, the condensed failure log, and the
   token/cost ledger.

## Human override switch

`lock_policy.human_override_active()` returns true when `SKIP_AGENT_HARNESS` is
set to a truthy value (`1`/`true`/`yes`/`on`). When active, the file-lock and
contract-binding hooks pass immediately, letting a human make sweeping
structural or configuration changes without the autonomous-agent gates blocking
them. The agent orchestrator never sets it, and the LLM seam **strips it from
the subprocess environment** (even in full-copy mode), so a value inherited from
the parent env cannot disable the hooks for git commands the agent itself spawns.

The same rule covers the orchestrator's **own** commits: it builds their git
environment through `_commit_env()`, which sets `AGENT_TASK_ID` and **drops
`SKIP_AGENT_HARNESS`** so the override can never disable the lock / contract
gates during an autonomous run. If the switch is set when a run starts,
`initialize` logs a one-time warning that it is being ignored for the run's
commits.
