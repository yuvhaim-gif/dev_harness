# The ledger, lock model & enforcement

> **Relevant source:** [`../../AGENTS.md`](../../AGENTS.md),
> [`../ledger.py`](../ledger.py),
> [`../validate_agents_ledger.py`](../validate_agents_ledger.py),
> [`../lock_policy.py`](../lock_policy.py),
> [`../enforce_file_locks.py`](../enforce_file_locks.py),
> [`../hook_context.py`](../hook_context.py).

## The operational ledger (`AGENTS.md`)

`AGENTS.md` holds **YAML** (despite the `.md` extension) and defines every task an
agent is allowed to run. Two example tasks ship with the project:

```yaml
schema_version: 1

tasks:
  add_payments_endpoint:
    description: >
      Add a POST /payments endpoint to the billing service.
    mutation_mode: evolve          # evolve = may edit spec_docs, tests, targets
    spec_docs:                     # OKF concept bundle (each file carries a non-empty 'type')
      [harness/example/docs/IMPLEMENTATION.md, harness/example/docs/API_SCHEMA.md,
       harness/example/docs/index.md, harness/example/docs/log.md]
    contracts:    [harness/example/docs/API_SCHEMA.md]  # stable, hash-pinned (⊆ spec_docs)
    tests:        [harness/example/tests/test_payments.py]
    contract_tests: [harness/example/tests/test_payments.py]  # pins the contract (⊆ tests)
    targets:      [harness/example/src/billing/routes.py, harness/example/src/billing/models.py]
    locked_files: []                            # AGENTS.md is ALWAYS locked implicitly
    commit_prefix: "feat"
    max_autorepair_attempts: 3
    pr_labels:    ["feature", "billing"]

  optimise_query_layer:
    description: >
      Replace N+1 patterns with batch fetches. No API contract changes.
    mutation_mode: isolated        # isolated = ONLY files in targets may change
    spec_docs:    [harness/example/docs/IMPLEMENTATION.md]
    tests:        [harness/example/tests/test_queries.py]
    targets:      [harness/example/src/db/queries.py]
    locked_files: [harness/example/docs/IMPLEMENTATION.md, harness/example/tests/test_queries.py]
    commit_prefix: "perf"
    max_autorepair_attempts: 3
    pr_labels:    ["performance"]
```

Recognised task fields: `description`, `mutation_mode`, `spec_docs`, `contracts`,
`tests`, `contract_tests`, `targets`, `locked_files`, `commit_prefix`,
`max_autorepair_attempts`, `pr_labels`. The ledger validator additionally
requires `contracts ⊆ spec_docs`, `contract_tests ⊆ tests`, every `spec_docs`
entry to be an OKF markdown concept (`.md`), and no `contracts` entry to be an
OKF reserved file (`index.md` / `log.md`). See
[The OKF information layer](okf-and-contracts.md#the-okf-information-layer).

### Lock model

The allowlist is computed once, in `harness/lock_policy.py`
(`compute_allowlist`), and imported by **both** the hook and the runner so they
can never drift:

| `mutation_mode` | Allowed to change |
|-----------------|-------------------|
| `evolve`   | `targets ∪ tests ∪ spec_docs ∪ {.harness/contracts.lock}` |
| `isolated` | `targets` only |

`evolve` may intentionally revise a contract, so the hashed contract manifest
(`.harness/contracts.lock`) is co-editable in that mode. `isolated` cannot
touch the manifest, so any contract drift it causes is left to fail the
contract tests.

After computing the allowlist, anything in `locked_files` is removed, and the
**always-locked** set is removed unconditionally:

```
AGENTS.md, .pre-commit-config.yaml
```

**Coordination paths bypass the allowlist — but only when well-formed.** Files
under `.harness/leases/` and `.harness/journal/` are written and committed by the
orchestrator itself (never by the LLM mutation), so `is_coordination_path()`
exempts them from the per-task allowlist. That exemption is **content-aware**:
`is_valid_coordination_payload()` admits a path only when it is a flat `*.json`
object directly under the coordination dir whose top-level keys are a subset of
the known lease/journal schema. An arbitrary file (a stray `.py`), a nested path,
or unknown-shaped JSON smuggled under the exempt prefix is **rejected** — by the
pre-commit hook, the post-hoc containment gate, and the server-side CI re-check
alike — so the exemption cannot be abused to land payload outside the allowlist
(notably as a prompt-injection vector via poisoned journal entries).

All ledger paths must be **POSIX** (forward-slash), repo-root-relative, because
that is exactly what `git diff --cached --name-only` emits on every OS.

**Non-regular modes (symlinks, gitlinks) are rejected by file *mode*, not just
path.** The allowlist reasons about path strings, but an allowlisted path can be
flipped from a regular file
(git mode `100644`) into a **symlink** (mode `120000`) aimed at a locked file —
keeping its permitted name while aliasing locked content. A **gitlink** (mode
`160000`) is the same escape: the allowlisted path becomes a submodule pointer
whose content lives out-of-band and never appears as a reviewable blob. A
path-only check would wave both through. Every lock layer therefore also inspects
the *resulting git mode* via a shared `lock_policy.symlink_paths()` helper (a
`--raw` diff parse) and rejects any agent-introduced non-regular tree entry —
anything whose new mode is neither a regular blob (`100644`/`100755`) nor a
deletion — outright, regardless of where it points. Reading the mode from git's
recorded tree entry — rather than `os.path.islink` — makes the check portable and
effective even against committed history on a CI runner where the entry is never
materialised on disk.

## How enforcement works

`harness/enforce_file_locks.py` runs as a `pre-commit` hook:

1. If `AGENT_TASK_ID` is **not** set → exit 0. Humans committing normally are
   never gated.
2. Load `AGENTS.md`; a missing file or invalid YAML aborts cleanly with a clear
   message (no traceback).
3. Look up the task and compute its allowlist.
4. Reject any **staged symlink or gitlink** outright — a `git diff --cached
   --raw` pass flags every entry whose resulting git mode is non-regular (not
   `100644`/`100755` and not a deletion), i.e. a symlink (`120000`) or a gitlink
   (`160000`). An allowlisted path flipped into one keeps its permitted name, so a
   path-only check would miss it; this closes the alias-to-a-locked-file and
   out-of-band-submodule bypasses and **exits 1**.
5. Compare the remaining staged files (`git diff --cached --name-only`) against
   the allowlist. Any staged file outside it → print the violations and
   **exit 1**, aborting the commit.

The orchestrator sets `AGENT_TASK_ID` only for its own commit subprocess, so the
gate is active for agent commits and transparent for everyone else.
