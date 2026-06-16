# Atomized Development Plan — Agent Workflow Framework

A generic, hardened framework that keeps automated/LLM coding agents "on the rails"
using a strict 5-state loop: **Initialize → Isolate → Mutate → Enforce → Autorepair/Reconcile**,
with programmatic file-locking to protect core assets.

This document is the build plan. Each step is **atomic** (independently doable),
**ordered**, and has an explicit **Done-when** check.

---

## 0. Preconditions (validated)

- **Write access**: confirmed — folder is writable, `plan.md` is not read-only.
- **Repo state**: this folder is **not yet a git repository** (`git status` → "not a git repository").
- **Toolchain present**: Git 2.53, Python 3.13, pip available.
- **Default-branch caveat (validated)**: on this machine `init.defaultBranch` resolves to
  `master`, and a plain `git init` produces `master`. The framework assumes `main`, so
  **A1 must force it** (`git init -b main`). Do not rely on a bare `git init`.
- **Branch-name caveat (validated)**: `git check-ref-format` **rejects** ISO-8601
  timestamps (`:` and `+` are illegal in refs). The work-branch stamp must be colon-free
  (see E3).
- **Action implied**: Step A1 below initializes git, since the framework is git-driven.

---

## Phase A — Repository & Environment Bootstrap

### A1. Initialize the git repository (force `main`)
- **Do**: `git init -b main` in the project root.
  - Rationale: plain `git init` honors the local `init.defaultBranch` (here: `master`),
    so the `-b main` flag is **mandatory**, not cosmetic.
  - On Git < 2.28 (no `-b`): run `git init` then `git branch -m main`.
- **Done-when**: `git symbolic-ref --short HEAD` prints `main` (and `git status` reports
  "On branch main / No commits yet").

### A2. Create an isolated virtual environment
- **Do**: `python -m venv .venv` and activate it
  (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on POSIX).
- **Do**: add `.venv/` to a `.gitignore` at the repo root.
- Rationale: the system interpreter is a restricted Windows Store Python; a venv keeps
  `pip install` reliable and the toolchain reproducible.
- **Done-when**: `python -c "import sys; print(sys.prefix)"` points inside `.venv` and
  `.gitignore` contains `.venv/`.

### A3. Create the project skeleton
- **Do**: create the directory/file layout:
  ```
  your-repo/
  ├── .gitignore
  ├── .pre-commit-config.yaml
  ├── AGENTS.md
  ├── agent_runner.py
  ├── requirements.txt
  └── scripts/
      └── hooks/
          ├── enforce_file_locks.py
          ├── validate_agents_ledger.py
          └── lock_policy.py          # shared compute_allowlist() (see B2)
  ```
- **Done-when**: all paths exist (empty placeholders are fine).

### A4. Pin dependencies
- **Do**: create `requirements.txt`:
  ```
  pyyaml>=6.0
  types-PyYAML>=6.0
  gitpython>=3.1
  pre-commit>=3.2        # 3.2.0 introduced the `pre-commit` stage name used in D1
  ```
- **Do**: `pip install -r requirements.txt` (inside the activated venv from A2).
- Note: `gitpython` is used by the orchestrator (C/E) only. The hook deliberately shells
  out to `git` instead, to keep pre-commit's isolated hook venv light (see C1/D1).
- **Done-when**: `python -c "import yaml, git"` exits 0 and `pre-commit --version` prints
  a version `>= 3.2.0`.

---

## Phase B — The State Ledger (`AGENTS.md`)

> Note: `AGENTS.md` holds **YAML content**. The hook/runner parse it with `yaml.safe_load`.

### B1. Define the schema + example tasks
```yaml
schema_version: 1

tasks:
  add_payments_endpoint:
    description: >
      Add a POST /payments endpoint to the billing service.
      Accepts amount, currency, and user_id. Returns a transaction_id.
    mutation_mode: evolve          # evolve = may edit spec_docs, tests, targets
    spec_docs:
      - docs/IMPLEMENTATION.md
      - docs/API_SCHEMA.md
    tests:
      - tests/test_payments.py
    targets:
      - src/billing/routes.py
      - src/billing/models.py
    locked_files: []               # AGENTS.md is ALWAYS locked implicitly
    commit_prefix: "feat"
    max_autorepair_attempts: 3
    pr_labels: ["feature", "billing"]

  optimise_query_layer:
    description: >
      Optimise the database query layer in src/db/queries.py.
      Replace N+1 patterns with batch fetches. No API contract changes.
    mutation_mode: isolated        # isolated = ONLY files in targets may change
    spec_docs:
      - docs/IMPLEMENTATION.md
    tests:
      - tests/test_queries.py
    targets:
      - src/db/queries.py
    locked_files:
      - docs/IMPLEMENTATION.md
      - tests/test_queries.py
    commit_prefix: "perf"
    max_autorepair_attempts: 3
    pr_labels: ["performance"]
```
- **Done-when**: `python -c "import yaml;yaml.safe_load(open('AGENTS.md'))"` exits 0.

> Integrity caveat: `AGENTS.md` carries a `.md` extension, so the `check-yaml` hook in D1
> (filtered to `\.(yaml|yml)$`) will **not** validate it. Because the whole framework
> depends on this file parsing, D1 adds a dedicated `validate-agents-ledger` hook and F4
> adds a standalone check.

### B2. Lock-model decision (hardening)
- **evolve**: allowlist = `targets ∪ tests ∪ spec_docs`. Everything else is implicitly locked.
- **isolated**: allowlist = `targets` only. `tests`, `spec_docs`, and all else are locked.
- `AGENTS.md` and `.pre-commit-config.yaml` are **always locked** for the agent.
- **Path normalization rule**: all ledger paths MUST be POSIX (forward-slash),
  repo-root-relative, because `git diff --cached --name-only` emits exactly that on every
  OS. The hook and runner compare against this canonical form (see C1/E5).
- **Anti-drift requirement**: encode this policy **once** in a small pure function
  (e.g. `compute_allowlist(task) -> set[str]`) and import it from **both** the hook and the
  runner, rather than copy-pasting the branch logic into each. Duplicated policy is the
  most likely source of a hook/runner mismatch.
- **Done-when**: the same `compute_allowlist` produces identical allowlists when called
  from the hook (C/D) and the runner (E) for both example tasks.

---

## Phase C — Enforcement Hook (`scripts/hooks/enforce_file_locks.py`)

This interceptor runs at pre-commit time and aborts the commit if a staged file
falls outside the task's allowlist (i.e. is locked).

### C1. Implement allowlist-based enforcement (hardened)
```python
#!/usr/bin/env python3
"""Abort commits that stage files outside the active task's allowlist."""
from __future__ import annotations
import os
import sys
import subprocess
import yaml

ALWAYS_LOCKED = {"AGENTS.md", ".pre-commit-config.yaml"}


def _staged_files() -> list[str]:
    # git emits POSIX-style, repo-root-relative paths on every OS.
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print(f"ERROR: could not read git index: {res.stderr.strip()}")
        sys.exit(1)
    return [line for line in res.stdout.splitlines() if line]


def main() -> None:
    # Humans committing normally (no agent context) are not gated.
    task_id = os.getenv("AGENT_TASK_ID")
    if not task_id:
        sys.exit(0)

    try:
        with open("AGENTS.md") as f:
            ledger = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print("ERROR: Missing operational ledger: AGENTS.md")
        sys.exit(1)
    except yaml.YAMLError as exc:
        # A malformed ledger must abort cleanly, not dump a traceback.
        print(f"ERROR: AGENTS.md is not valid YAML: {exc}")
        sys.exit(1)

    task = (ledger.get("tasks") or {}).get(task_id)
    if not task:
        print(f"ERROR: Task '{task_id}' not found in AGENTS.md.")
        sys.exit(1)

    mode = task.get("mutation_mode")
    # `... or []` guards a present-but-null key (YAML `targets:` -> None),
    # which would otherwise make set(None) raise TypeError.
    targets = set(task.get("targets") or [])
    tests = set(task.get("tests") or [])
    spec_docs = set(task.get("spec_docs") or [])

    if mode == "evolve":
        allowed = targets | tests | spec_docs
    elif mode == "isolated":
        allowed = set(targets)
    else:
        print(f"ERROR: Unknown mutation_mode '{mode}' for task '{task_id}'.")
        sys.exit(1)

    # Explicit locks always win, even if mistakenly present in the allowlist.
    explicit_locked = set(task.get("locked_files") or []) | ALWAYS_LOCKED
    allowed -= explicit_locked

    violations = sorted(f for f in _staged_files() if f not in allowed)
    if violations:
        print(f"ERROR: task '{task_id}' ({mode}) staged files outside its allowlist:")
        for v in violations:
            print(f"  - {v}")
        print("Allowed:", ", ".join(sorted(allowed)) or "(none)")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
```
- **Hardening vs. original**: switched from a *denylist* (only blocks named locked
  files) to an *allowlist* (blocks anything not explicitly permitted), removed the
  uncaught `check=True` traceback, and always-lock `.pre-commit-config.yaml`.
- **Hardening vs. prior draft of this plan**: (a) catch `yaml.YAMLError` so a corrupt
  ledger aborts cleanly; (b) use `task.get(<key>) or []` so a present-but-null list does
  not crash on `set(None)`.
- **Done-when**: with `AGENT_TASK_ID` set and an out-of-scope file staged, the script
  exits `1`; with only allowed files staged, it exits `0`; with a deliberately corrupted
  `AGENTS.md`, it prints the YAML error and exits `1` (no traceback).

---

## Phase D — Pre-commit Configuration (`.pre-commit-config.yaml`)

### D1. Wire syntax, lint/type, and the local lock hook
```yaml
minimum_pre_commit_version: "3.2.0"   # `pre-commit` stage name requires >= 3.2.0
default_stages: [pre-commit]

repos:
  # LAYER 1: SYNTAX & FORMAT
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: check-merge-conflict
      - id: check-yaml
        files: \.(yaml|yml)$
      - id: check-json
        files: \.json$
      - id: check-added-large-files
        args: ["--maxkb=500"]
      - id: trailing-whitespace
      - id: end-of-file-fixer

  # LAYER 2: LINT & TYPES
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.4.4
    hooks:
      - id: ruff
        args: ["--fix"]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.10.0
    hooks:
      - id: mypy
        args: ["--strict", "--ignore-missing-imports"]

  # LAYER 3: LEDGER INTEGRITY + FILE-LOCK ENFORCEMENT
  - repo: local
    hooks:
      - id: validate-agents-ledger
        name: "Validate AGENTS.md is loadable YAML with required keys"
        language: python
        entry: python scripts/hooks/validate_agents_ledger.py
        additional_dependencies: ["pyyaml>=6.0"]
        files: ^AGENTS\.md$
        pass_filenames: false

      - id: enforce-file-locks
        name: "Enforce agent file locks"
        language: python
        entry: python scripts/hooks/enforce_file_locks.py
        additional_dependencies: ["pyyaml>=6.0"]
        pass_filenames: false
        always_run: true
```
- **Hardening vs. original**: added `additional_dependencies: pyyaml` so the local hook
  has its import available inside pre-commit's isolated venv.
- **Hardening vs. prior draft of this plan**:
  - bumped `minimum_pre_commit_version` to `3.2.0` to match the `pre-commit` stage name in
    `default_stages` (the old `commit` stage name was renamed in 3.2.0); without this, the
    config is invalid on pre-commit 3.0–3.1.
  - added a `validate-agents-ledger` hook because `check-yaml` is filtered to
    `\.(yaml|yml)$` and therefore never inspects the YAML-bearing `AGENTS.md`.
- **`validate_agents_ledger.py` contract**: `yaml.safe_load(open("AGENTS.md"))`, assert a
  `tasks` mapping exists, and assert every task has a valid `mutation_mode`
  (`evolve`|`isolated`); print a clear error and exit `1` otherwise.
- **Done-when**: `pre-commit install` succeeds and `pre-commit run --all-files` runs all
  layers, including both local hooks.

---

## Phase E — Orchestrator (`agent_runner.py`)

Implement the 5-state loop. Each sub-step is atomic and testable in `--dry-run`.

### E1. Data models
- `TaskSpec` (parsed task) and `RunContext` (mutable run state) dataclasses.
- **Done-when**: module imports cleanly under `mypy --strict`.

### E2. State 1 — Initialize (hardened)
- Open repo via `git.Repo(search_parent_directories=True)`.
- Refuse to run on a **dirty** working tree.
- Pull from `origin` **only if a remote exists and the branch tracks it**; otherwise log
  and continue (fixes original crash when no `origin` is configured).
- Parse `AGENTS.md`; reject `schema_version` newer than supported.
- **Done-when**: `--dry-run` on a clean repo with no remote completes without raising.

### E3. State 2 — Isolate
- Record the original branch name (for E6 rollback) before switching.
- Create branch `agent/<task_id>/<UTC-stamp>` where the stamp is
  **`datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")`**.
  - ⚠️ **Do NOT use `isoformat()`**: it emits `:` and `+`, which `git check-ref-format`
    rejects (validated: `fatal: ... is not a valid branch name`). The `strftime` form above
    is colon-free and passes.
  - Defensively run `git check-ref-format --branch <name>` (or `Repo.git.check_ref_format`)
    before creating the branch.
- Verify declared `tests` exist; in `isolated` mode verify `targets` exist.
- **Done-when**: the computed branch name passes `git check-ref-format`, the branch is
  created (or, in dry-run, only computed), and path checks pass.

### E4. State 3 — Mutate (dispatch)
- Dispatch on `mutation_mode` to `evolve` (spec → tests → source) or `isolated`
  (source-in-`targets` only). These are the **LLM integration seams** (left as hooks).
- **Done-when**: dispatcher routes correctly and raises on unknown modes.

### E5. State 4 — Enforce (hardened)
- Compute the staging set from the **shared** `compute_allowlist(task)` (B2), then stage
  **only those paths** (not `git add -A`) so untracked logs/artifacts and locked files
  never enter the index. This prevents self-inflicted lock violations.
- **Normalize paths to POSIX** before `git add` (replace `\` with `/`) so they match the
  ledger and `git diff --cached --name-only` output on Windows.
- **Dry-run guard**: if `--dry-run`, log the intended staging set and **return without
  committing** (so F3's "no commits created" holds).
- Inject `AGENT_TASK_ID` into the commit subprocess env so the hook knows the task.
- Run `git commit`; capture stdout+stderr; classify the result:
  - **`passed`** — commit succeeded.
  - **`mechanical`** — an auto-fixing hook (`ruff --fix`, `trailing-whitespace`,
    `end-of-file-fixer`) rewrote an allowlisted file ("files were modified by this hook").
    This is NOT a semantic failure: re-stage the same allowlist and retry **once**; it
    must not consume an autorepair attempt.
  - **`semantic`** — lint/type/lock failure that needs a real fix; hand to E6.
- Return `(status, log)`.
- **Done-when**: a clean change commits; a whitespace-only diff auto-fixes and commits on
  the mechanical retry without touching the autorepair counter; a staged locked file is
  rejected by the hook as `semantic`.

### E6. State 5A — Autorepair (semantic only)
- Triggered only by an E5 `semantic` failure. Increment the attempt counter; if
  `> max_autorepair_attempts`, **escalate and roll back**: `git checkout <original branch>`
  (captured in E3) and optionally delete the work branch, then exit non-zero.
- Otherwise feed `last_hook_log` back to the fix loop (LLM seam), then re-enter E5.
- **Done-when**: exceeding the cap escalates and restores the original branch without
  leaving a half-committed work branch checked out.

### E7. State 5B — Reconcile (honest)
- **Dry-run guard**: if `--dry-run`, log the push/PR commands that *would* run and return.
- **Remote guard**: push only if an `origin` remote exists (consistent with E2's no-remote
  tolerance). If absent, log the exact manual `git push` command and skip — do not crash.
- Otherwise `git push -u origin <branch>`.
- Open a PR **only if** the GitHub CLI is available (`gh pr create` with labels);
  otherwise log the exact manual PR command instead of falsely claiming a PR was created
  (fixes the misleading original log).
- **Done-when**: with a remote, the branch is pushed and a PR is created via `gh` or a
  clear manual hint is logged; with **no** remote, a manual-push hint is logged and the run
  exits `0` without raising.

### E8. CLI / main loop
- `argparse`: `--task` (default `AGENT_TASK_ID`), `--dry-run`.
- Loop: `mutate → enforce → (mechanical? re-stage+retry once) → (semantic? autorepair) → reconcile`.
- Wrap in try/except with non-zero exit on unhandled error; on any failure after E3,
  attempt the E6 rollback so a partial run never leaves a stray checked-out work branch.
- **Done-when**: `python agent_runner.py --task add_payments_endpoint --dry-run` runs
  end-to-end on a clean, remote-less repo with no commits and no exceptions.

---

## Phase F — Verification

### F1. Static checks
- **Do**: `ruff check .`, `ruff format --check .`, `mypy --strict agent_runner.py scripts`.
- **Done-when**: all pass with zero errors.

### F2. Lock-hook behavior tests
- **F2a (block)**: set `AGENT_TASK_ID=optimise_query_layer`, stage `tests/test_queries.py`
  → commit **must abort** (test file is locked in isolated mode).
- **F2b (allow)**: stage only `src/db/queries.py` → commit **must succeed**.
- **F2c (human bypass)**: unset `AGENT_TASK_ID`, stage any file → hook **must pass**.
- **F2d (corrupt ledger)**: set `AGENT_TASK_ID`, temporarily break `AGENTS.md` YAML
  → hook prints a YAML error and exits `1` **with no traceback**.
- **F2e (null list)**: a task whose `targets:` is empty/null must not crash the hook.
- **Done-when**: all five behave as specified.

### F3. Dry-run smoke test
- **Do**: run the orchestrator in `--dry-run` for both example tasks on the clean,
  remote-less repo.
- **Done-when**: both reach Reconcile with no exceptions, **no commits created**, and the
  no-remote manual-push hint is logged.

### F4. Branch-name & ledger guards (validated)
- **F4a (branch name)**: assert the computed work-branch name passes
  `git check-ref-format --branch <name>`; assert an `isoformat()`-style name is **rejected**
  (regression guard for the colon/`+` bug).
- **F4b (ledger validator)**: run `python scripts/hooks/validate_agents_ledger.py`
  → exits `0` on the shipped `AGENTS.md`, `1` on a corrupted copy.
- **Done-when**: both guards behave as specified.

---

## Phase G — Operator Runbook

```bash
# 0. Initialize git on the main branch (do NOT rely on bare `git init`)
git init -b main

# 1. Create + activate a virtualenv, then install dependencies
python -m venv .venv
# Windows:  .venv\Scripts\activate
# POSIX:    source .venv/bin/activate
pip install -r requirements.txt

# 2. Register the pre-commit hooks into local git
pre-commit install

# 3. Run the workflow for a task defined in AGENTS.md
python agent_runner.py --task add_payments_endpoint
```

---

## Hardening Summary (issues found vs. original draft)

1. **Lock model**: denylist → **allowlist** (anything not explicitly permitted is blocked).
2. **`git add -A`**: replaced with **scoped staging** so artifacts/locked files can't sneak in.
3. **`origin.pull()`**: now **conditional** on a configured, tracking remote (no crash on fresh repos).
4. **`datetime.utcnow()`**: replaced with timezone-aware **`datetime.now(timezone.utc)`**.
5. **Fake "PR created" log**: replaced with real **`gh pr create`** or an honest manual hint.
6. **Hook robustness**: removed uncaught `check=True` traceback; handle missing/empty ledger.
7. **Always-locked set**: now includes **`.pre-commit-config.yaml`** alongside `AGENTS.md`.
8. **Failure rollback**: autorepair exhaustion now **restores branch state** before exiting.
9. **Hook deps**: local hook declares **`additional_dependencies: pyyaml`** for pre-commit's venv.

## Second-pass Hardening (validated against Git 2.53 on this machine)

10. **Default branch (VALIDATED bug)**: bare `git init` here yields `master`
    (`init.defaultBranch=master`); A1/runbook now force **`git init -b main`**.
11. **Illegal branch name (VALIDATED bug)**: `isoformat()` stamps contain `:`/`+` and are
    **rejected** by `git check-ref-format`; E3 now mandates
    **`strftime("%Y%m%dT%H%M%SZ")`** and a pre-flight ref-format check.
12. **pre-commit version mismatch**: `default_stages: [pre-commit]` needs **>= 3.2.0**;
    `minimum_pre_commit_version` and `requirements.txt` bumped accordingly.
13. **Mechanical vs. semantic failures**: auto-fixer rewrites ("files were modified by this
    hook") now trigger a **re-stage+retry**, not an LLM autorepair attempt.
14. **Reconcile/no-remote**: push is now **guarded on `origin`**, consistent with Initialize;
    no crash on a remote-less repo.
15. **Dry-run integrity**: explicit dry-run guards in E5/E7 keep F3's "no commits" honest.
16. **Ledger integrity**: `AGENTS.md` (a `.md` file) is now validated by a dedicated
    **`validate-agents-ledger`** hook, since `check-yaml` skips it.
17. **Hook null-list crash**: `set(task.get(<key>) or [])` prevents `set(None)` on empty keys;
    `yaml.YAMLError` is caught for corrupt ledgers.
18. **Path normalization**: ledger/staging paths standardized to **POSIX `/`** to match
    `git diff --cached` output cross-OS.
19. **Anti-drift**: lock policy centralized in a shared **`compute_allowlist()`** imported by
    both hook and runner.
20. **Environment isolation**: a **virtualenv** step (A2) precedes `pip install` on the
    restricted Windows Store Python.
