# Testing, the pre-commit pipeline & design notes

> **Relevant source:** [`../tests/test_harness.py`](../tests/test_harness.py),
> [`../tests/test_hardening.py`](../tests/test_hardening.py),
> [`../tests/test_okf.py`](../tests/test_okf.py),
> [`../tests/test_contracts.py`](../tests/test_contracts.py),
> [`../../.pre-commit-config.yaml`](../../.pre-commit-config.yaml),
> [`../runner_llm.py`](../runner_llm.py),
> [`../runner_states.py`](../runner_states.py),
> [`../runner_recovery.py`](../runner_recovery.py).

## Testing & verification

Run everything:

```bash
ruff check .
ruff format --check .
mypy --strict harness
pytest -q
```

`harness/tests/test_harness.py` verifies the **framework itself** (it builds
throwaway git repos in a temp dir):

| Group | Test | Verifies |
|-------|------|----------|
| **F2** lock hook   | `test_f2a_blocks_locked_file_in_isolated_mode`        | A locked file is rejected (exit 1, no traceback). |
| **F2**             | `test_f2b_allows_target_file`                          | An allowlisted file commits cleanly. |
| **F2**             | `test_f2c_human_bypass_without_task`                   | No `AGENT_TASK_ID` → hook passes. |
| **F2**             | `test_f2d_corrupt_ledger_aborts_cleanly`               | Broken YAML → clean error, no traceback. |
| **F2**             | `test_f2e_null_target_list_does_not_crash`             | A null `targets:` does not crash the hook. |
| **F3** orchestrator| `test_f3_dry_run_reaches_reconcile_without_commits`    | Dry-run (both tasks): no commits, no branch, manual-push hint. |
| **F4** branch/ledger | `test_f4a_computed_branch_name_is_valid`             | The work-branch name passes `git check-ref-format`. |
| **F4**             | `test_f4a_isoformat_branch_name_is_rejected`           | An `isoformat()` name (with `:` / `+`) is rejected. |
| **F4**             | `test_f4b_validator_*`                                 | Validator passes on a good ledger, fails on a corrupt one. |
| **F5** coordination| `test_f5_coordination_paths_bypass_allowlist`          | A *well-formed* `.harness/leases/`/`.harness/journal/` payload is commit-allowed. |
| **F6** binding     | `test_f6a_contract_change_without_manifest_is_blocked` | Contract change without manifest update → exit 1. |
| **F6**             | `test_f6b_contract_change_without_bound_test_is_blocked` | Contract change without a bound test → exit 1. |
| **F6**             | `test_f6c_contract_change_with_manifest_and_test_passes` | Contract + manifest + bound test in one commit → ok. |
| **F6**             | `test_f6d_non_contract_change_is_not_gated`            | Non-contract edits are not gated by the binding hook. |
| **F7** manifest    | `test_f7_manifest_detects_drift`                       | `contract_manifest.verify()` reports a drifted hash. |
| **F8** leases      | `test_f8_lease_blocks_second_agent_then_releases`      | A live lease blocks a second agent; release re-opens it. |
| **F8**             | `test_acquire_single_winner_under_concurrent_reclaim`  | 30 threads racing one expired lease → **exactly one** wins (reclaim-mutex serialisation; no TOCTOU race). |
| **F8**             | `test_is_active_nonnumeric_ttl_is_inactive`            | A non-numeric `ttl_seconds` is treated as inactive, not an uncaught `ValueError`. |
| **F9** journal     | `test_f9_journal_records_unresolved_for_next_agent`    | Escalated sessions are recoverable via `latest_unresolved()`. |
| **F10** staleness  | `test_f10_staleness_detects_moved_contract`            | A contract moved on the shared ref is reported as stale. |
| **F10**            | `test_f10b_staleness_includes_task_targets`            | A task **target** moved on the shared ref is reported as stale (loser-of-lease backstop). |
| **F11** state sync | `test_f11_state_sync_round_trips_across_clones`        | Coordination state pushed to the shared ref is readable from a fresh clone. |
| **F12** state sync | `test_f12_publish_files_returns_false_on_unreachable_remote` | A push that cannot reach its remote returns `False` (no silent swallow). |
| **F13** manifest   | `test_f13_corrupt_lock_reports_cleanly`               | A corrupt `contracts.lock` yields an actionable message, not a traceback. |
| **F14** CI re-check| `test_f14a/b_ci_enforce_*_agent_branch`               | `ci_enforce.py` blocks an out-of-scope agent branch and passes an in-scope one. |
| **F14** CI binding | `test_f14c/d_ci_enforce_*_bound_test`                 | `ci_enforce.py` blocks a contract change that omits its bound `contract_tests` and passes one that includes them. |
| **F14** CI coord   | `test_f14e/f/g_ci_enforce_*_journal`                  | A **directly pushed** agent branch smuggling a `.py` or unknown-shaped JSON under `.harness/journal/` is rejected; a well-formed journal `*.json` passes (the no-SHA-backstop layer). |
| **F15** bootstrap  | `test_f15a_init_writes_empty_skeleton_that_validates` | `--init` writes a project README + an empty `AGENTS.md` skeleton that validates. |
| **F15**            | `test_f15b_init_example_recreates_shipped_ledger`     | `--init --example` reproduces the shipped example ledger byte-for-byte. |
| **F15**            | `test_f15c_doctor_flags_template_readme_then_init_clears_it` | `--doctor` warns on the template README; `--init` clears the sentinel. |
| **F16** drive loop | `test_f16a–h_*`                                        | The `run_drive` state machine, exercised with fakes: `passed`/`dry-run` reconcile, a single mechanical retry, post-mutate/post-repair aborts (exit 3/4), the autorepair cap (exit 1), and post-pass containment (exit 4). |
| **F17** CLI        | `test_f17a–d_*`                                        | `--version`, `--list`, `--report-json` (valid JSON with the expected keys), and `--release` clearing a local lease. |
| **F18** packaging  | `test_f18_editable_install_exposes_console_script`    | An editable install exposes the `agent-harness` console entry point. |

`harness/tests/test_hardening.py` covers the LLM-execution hardening layer: token-usage
normalisation and budget aborts (telemetry), log condensation, cache-ordered
prompt assembly, bypass-flag stripping **and hook-evasion flagging** (command
guard — including the `sh -c` / `bash -lc` / `cmd /c` script-wrapped bypass,
H4t–H4x), the `SKIP_AGENT_HARNESS` override,
forensic-report generation (including H5c, which asserts the step log shows
**genuinely per-attempt** costs), the
content-aware **coordination-payload validator** (`is_valid_coordination_payload`,
H5d) and the immutable rule tagging handover/journal text as **untrusted data**
(H5e), an end-to-end financial-abort run that asserts the exit-3 path rolls back
and writes `FAILED_AGENT_RUN.md`, and an end-to-end **containment-breach** run
(H8) where an LLM that commits an out-of-band file via a hook bypass is caught by
the post-hoc gate and aborts with **exit 4** plus a clean rollback. A dedicated
**symlink / gitlink bypass** group (H8b–H8d) covers the mode-aware lock: the
`symlink_paths()` `--raw` parser (flagging symlinks *and* gitlinks while leaving
regular-file mode changes and deletions alone), the pre-commit hook blocking an
allowlisted path staged as a symlink to a locked file or as a gitlink (exit 1),
and the post-hoc gate reporting either as a breach even when its path is itself
allowlisted. The **rollback**
group (H9/H9b) asserts agent-created untracked files are removed — *including*
LLM junk under `.harness/` (a stray `.py`, malformed JSON) — while the harness's
own logs, telemetry, manifest, and well-formed coordination payloads are kept.

`harness/tests/test_contracts.py::test_contracts_match_manifest` additionally asserts
that the shipped `.harness/contracts.lock` matches every declared contract, and
`test_hash_is_stable_and_covers_okf_frontmatter` pins that the contract hash is
whole-file (OKF frontmatter included), so silently editing a contract's `type`
still trips the manifest.

`harness/tests/test_okf.py` covers the OKF info layer directly: the frontmatter
parser, the `type` gate, malformed-YAML handling, the contract `timestamp`
prohibition, the reserved-file rules (`index.md` `okf_version`-only, `log.md`
lenient), `spec_map_from_ledger`, and live-ledger conformance. `test_hardening.py`
adds the forensic-memory checks (H5f: the post-mortem is a valid OKF `Postmortem`
concept; H5g: `log.md` stays dated and newest-first) and the drive-machine
containment checks (H14/H14b: a committed spec_doc with stripped frontmatter is
caught by `_okf_violations` + the post-hoc gate with **exit 4**, while a
conformant doc is not flagged).

The remaining tests (`test_payments.py`, `test_queries.py`) cover the sample
workload's contracts.

## Pre-commit pipeline

`.pre-commit-config.yaml` runs, in order:

1. **Syntax/format** — merge-conflict, `check-yaml` (`.yaml`/`.yml`), `check-json`
   (`.json`), large-file guard (`--maxkb=500`), trailing-whitespace,
   end-of-file-fixer.
2. **Lint & types** — `ruff` (`--fix`), `ruff-format`, `mypy --strict harness`.
3. **Ledger integrity, file locks & contract binding** —
   - `validate-agents-ledger` (because `check-yaml` skips the `.md`-extensioned
     `AGENTS.md`),
   - `enforce-file-locks` (no staged file may fall outside the task's allowlist,
     and no staged entry may be a symlink or gitlink),
   - `verify-contract-manifest` (every declared contract still matches
     `.harness/contracts.lock`),
   - `enforce-contract-binding` (a staged contract change must co-stage the
     manifest and a bound test),
   - `validate-okf` (every declared `spec_doc` must be an OKF-conformant concept
     — a non-empty `type`, no contract `timestamp`, reserved-file rules honoured).

## Design notes / hardening

- **Allowlist, not denylist** — anything not explicitly permitted is blocked.
- **Scoped staging** — the runner stages only allowlisted paths, so stray
  artifacts and locked files can never enter the index.
- **Colon-free branch stamps** — `strftime("%Y%m%dT%H%M%SZ")`, because
  `isoformat()` emits `:`/`+`, which `git check-ref-format` rejects.
- **No-remote tolerant** — Initialize and Reconcile both guard on `origin`, so a
  fresh, remote-less repo never crashes.
- **Honest reconcile** — a PR is opened via `gh` or an exact manual command is
  printed; the framework never falsely claims a PR was created. A push that fails
  after a clean local run is re-journaled to a recoverable `error` (work retained
  locally, lease released) so a re-run retries it rather than reporting `pushed`.
- **Mechanical vs. semantic** — a failed commit is classified by inspecting
  whether an auto-fixer **dirtied the worktree** (not by string-matching English
  hook wording); a mechanical rewrite triggers a single re-stage+retry and does
  **not** consume an autorepair attempt.
- **Single source of truth** — `compute_allowlist()` is imported by both the hook
  and the runner to prevent policy drift.
- **Always-locked** — `AGENTS.md` and `.pre-commit-config.yaml` can never be
  modified by an agent task.
- **Mode-aware locks** — the path allowlist is backed by a file-*mode* check
  (`lock_policy.symlink_paths()`): every lock layer (pre-commit, post-hoc
  containment gate, CI re-check) rejects any agent-introduced non-regular tree
  entry — a symlink (git mode `120000`) or a gitlink (`160000`), i.e. anything
  that is not a regular blob (`100644`/`100755`) or a deletion — so an allowlisted
  path cannot be aliased onto a locked file or turned into an out-of-band
  submodule pointer. Mode is read from git's recorded tree entry, not
  `os.path.islink`, so it holds against committed history on a CI runner.
- **OKF information layer** — every task's `spec_docs` is an Open Knowledge
  Format concept bundle; `okf.verify()` enforces a non-empty `type` (and no
  volatile `timestamp` on contracts) at four layers (pre-commit, post-hoc
  containment gate, CI re-check, `--doctor`), and forensic post-mortems join the
  same layer as an OKF `Postmortem` concept plus a dated `log.md` history file.
- **Financial & time circuit-breaker** — cumulative token and USD budgets are
  checked after every LLM step, alongside per-step (`AGENT_STEP_TIMEOUT_SECONDS`)
  and wall-clock (`MAX_RUN_SECONDS`) time ceilings; a breach hard-rolls-back and
  exits 3 (with a reason distinguishing a budget breach from a timeout) before
  more spend.
- **Lean repair context** — raw tool dumps are condensed to failing assertions
  with a 3-line window, keeping the autorepair loop token-efficient.
- **Cache-ordered prompts** — repair prompts are assembled static→dynamic to
  maximise provider prompt-cache hits across recursive repairs.
- **Escape-hatch interception** — `--no-verify` / `-n` on a spawned `git commit`
  / `git push` are stripped and charged to a dedicated `guard_penalties` budget
  (separate from the autorepair counter, with its own exit-4 ceiling), and
  unstrippable hook-evasion (`core.hooksPath`, plumbing) is flagged. This raises
  the cost of a bypass but is **not** a security boundary; the authoritative
  defences are the post-hoc containment gate and the server-side CI re-check.
- **Scoped LLM environment** — `AGENT_ENV_ALLOWLIST` restricts which parent env
  vars reach the seam; when set, **only the explicitly named vars** pass (no
  `AGENT_*` / `GIT_*` prefix carve-out, so prefixed secrets are not leaked), while
  unset means a full copy plus a one-time warning, and the active `env_scope` is
  surfaced in forensics and `--doctor`. `SKIP_AGENT_HARNESS` is always dropped
  from the seam so the human override cannot be inherited.
- **Post-hoc containment gate** — after mutation the orchestrator inspects
  committed history (`base..HEAD`); any out-of-allowlist, symlink, gitlink, or
  out-of-band (hook-bypassed) commit triggers a forensic rollback and **exit
  4**, so a skipped local hook cannot smuggle work onto the branch.
- **Server-side re-enforcement** — `harness/ci_enforce.py` re-applies the
  allowlist + manifest check from a trusted CI runner the agent cannot influence.
- **Git-environment hardening** — the LLM seam runs with `GIT_CONFIG_NOSYSTEM`
  set and the whole inherited git-config env family dropped (`GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_PARAMETERS`, and the `git -c` env form
  `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*`), so stray or
  injected git config — including a `core.hooksPath` override the command guard
  would never see in the command string — cannot weaken the gates mid-run.
- **Fail-safe coordination** — `publish_files()` retries with backoff + jitter
  and surfaces failures (never silent); staleness deepens shallow clones and
  honours `AGENT_STALENESS_STRICT`.
- **Diagnosability** — `--doctor` reports the health of every coordination
  subsystem in one pass; `AGENT_MINIMAL=1` drops the shared-ref machinery for
  simple single-agent runs.
- **Human override** — `SKIP_AGENT_HARNESS=1` lets a developer bypass the gates
  for sweeping manual changes; the orchestrator never sets it, the LLM seam
  strips it from the subprocess env, and the runner's own commits go through
  `_commit_env()` which drops it too (with a one-time warning at `initialize`),
  so it cannot be inherited into any git command an autonomous run issues.
- **Forensic containment** — a rejected/crashed run leaves a transparent
  `.harness/logs/FAILED_AGENT_RUN.md` and a terminal badge, with the working
  tree verified clean.

## Extending the framework

- **Add a task** — add an entry under `tasks:` in `AGENTS.md` and run
  `validate-agents-ledger` (or commit) to confirm it parses.
- **Wire in an LLM** — implement the body of `_run_llm()` in
  `harness/runner_llm.py`; both `mutate()` (in `harness/runner_states.py`) and
  the fix step inside `autorepair()` (in `harness/runner_recovery.py`) funnel
  through it, and everything around those seams already handles isolation,
  enforcement, classification, rollback, and reconcile.
