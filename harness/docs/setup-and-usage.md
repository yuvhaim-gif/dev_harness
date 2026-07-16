# Setup, usage & the sample workload

> **Relevant source:** [`../../pyproject.toml`](../../pyproject.toml),
> [`../../.pre-commit-config.yaml`](../../.pre-commit-config.yaml),
> [`../runner_cli.py`](../runner_cli.py),
> [`../example/src/billing/routes.py`](../example/src/billing/routes.py),
> [`../example/src/billing/models.py`](../example/src/billing/models.py),
> [`../example/src/db/queries.py`](../example/src/db/queries.py),
> [`../example/docs/API_SCHEMA.md`](../example/docs/API_SCHEMA.md).

## Requirements

- **Python 3.12+** (uses `datetime.UTC`; ruff and mypy target `py312`).
- **Git 2.28+** (for `git init -b`; the project assumes the `main` branch).
- Python packages (declared in `pyproject.toml`):
  - runtime: `pyyaml`, `gitpython`, `pre-commit`
  - dev/verification extras (`pip install .[dev]`): `types-PyYAML`, `ruff`, `mypy`, `pytest`.
- Optional: GitHub CLI (`gh`) for automatic PR creation in Reconcile.

## Setup

> On Windows use `.venv\Scripts\...`; on POSIX use `.venv/bin/...`.

```bash
# 1. Initialize git on the main branch (do NOT rely on a bare `git init`)
git init -b main

# 2. Create + activate a virtualenv
python -m venv .venv
# Windows:  .venv\Scripts\activate
# POSIX:    source .venv/bin/activate

# 3. Install dependencies (the dev extra adds ruff / mypy / pytest)
pip install .[dev]

# 4. Register the pre-commit hook into local git
pre-commit install

# 5. Prime YOUR project: replace the template README + seed an empty AGENTS.md
python -m harness --init
```

> **Before you start:** the harness ships a placeholder root `README.md`
> carrying a `<!-- HARNESS_TEMPLATE_README` sentinel. `python -m harness --init`
> overwrites it with a project stub and writes an empty `AGENTS.md` skeleton
> (`tasks: {}`). `--doctor` keeps warning until the sentinel is gone, so it is
> hard to forget. Use `--init --example` to instead reproduce the bundled demo
> ledger (handy for self-checking the harness), and `--force` to overwrite an
> existing `AGENTS.md` / project README.
>
> On a fresh clone, `--doctor` prints two **expected** warnings, neither an
> error: this template-README reminder, and an `AGENT_ENV_ALLOWLIST not set`
> notice (without it the LLM subprocess inherits the full parent environment —
> set `AGENT_ENV_ALLOWLIST` to scope it). Note that *this* repository is the
> harness itself, so the template-README warning is expected here; it clears in
> any project that runs `--init`.

## Running the orchestrator

The framework is invoked as a module — `python -m harness` — which works both
in-place and once installed (`pip install .`). An install also exposes console
entry points — `agent-harness` (the orchestrator) and `agent-ci-enforce` (the
server-side re-check) — so `agent-harness --task …` is equivalent to
`python -m harness --task …`. The pre-commit hooks still invoke the modules by
path (`python harness/…`), so both invocation styles stay valid.

```bash
# Prime a fresh project (replace template README, seed empty AGENTS.md)
python -m harness --init

# Reproduce the bundled demo ledger instead of an empty skeleton
python -m harness --init --example

# Plan only — compute branch + staging set, never commit or push
python -m harness --task add_payments_endpoint --dry-run

# Real run for a task defined in AGENTS.md
python -m harness --task optimise_query_layer

# Health report of every coordination subsystem (no run performed)
python -m harness --doctor
```

CLI:

| Flag | Meaning |
|------|---------|
| `--task <id>` | Task id from `AGENTS.md`. Defaults to `$AGENT_TASK_ID`. |
| `--dry-run`   | Compute the branch name and staging set, log the push/PR commands, but make **no** commits, branches, or pushes. |
| `--init`      | Prime a fresh project: replace the template `README.md` with a project stub and seed an empty `AGENTS.md`, then exit. |
| `--example`   | With `--init`, seed the bundled example ledger (`harness/example/AGENTS.example.md`) instead of an empty skeleton. |
| `--force`     | With `--init`, overwrite an existing `AGENTS.md` / project `README.md`. |
| `--doctor`    | Print a one-pass [health report](containment-and-diagnostics.md#diagnostics---doctor) (manifest, leases, journals, shared ref, minimal mode, README sentinel) and exit; non-zero when a hard problem is found. |
| `--version`   | Print the harness version and exit. |
| `--list`      | List the tasks declared in `AGENTS.md` (id, mutation mode, target count) and exit. |
| `--report-json` | Print a JSON telemetry/outcome summary of the latest run (`version`, `task_id`, `outcome`, `branch`, `finished_at`, `total_tokens`, `cost_usd`) and exit. |
| `--release <id>` | Operator escape hatch: force-release a stranded lease for `<id>` (local + shared ref) and exit; prompts for confirmation unless `--yes`. |
| `--yes`       | Skip the confirmation prompt (used with `--release`). |

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Failure (e.g. autorepair cap exceeded, or a stale push refused); the tree is rolled back. |
| `2` | No task specified. |
| `3` | **Financial or time abort** — a token/cost budget was breached, or a step (`AGENT_STEP_TIMEOUT_SECONDS`) / wall-clock (`MAX_RUN_SECONDS`) timeout fired; the tree is rolled back and a forensic report (carrying the distinguishing reason) is written. |
| `4` | **Containment breach** — the agent committed outside its allowlist, committed a symlink or gitlink (aliasing an allowlisted path onto a locked file, or smuggling out-of-band submodule content), committed a spec_doc that breaks OKF conformance (stripped `type` / added contract `timestamp`), made an out-of-band (hook-bypassed) commit, or exhausted the `guard_penalties` ceiling with repeated git-bypass attempts; the tree is rolled back and a forensic report is written. |

Example dry-run output (remote-less repo):

```
[agent_runner] no tracking remote configured; skipping pull.
[agent_runner] initialized for task 'optimise_query_layer' (mode=isolated, agent=agent-9f3a2c10).
[agent_runner] [dry-run] computed work branch 'agent/optimise_query_layer/20260616T131752Z-3e5b1d' (not created).
[agent_runner] [mutate] isolated: source-in-targets only (LLM integration seam).
[agent_runner] [dry-run] would stage exactly: ['harness/example/src/db/queries.py']
[agent_runner] [dry-run] skipping commit to keep 'no commits created' honest.
[agent_runner] [dry-run] would run: git push -u origin agent/optimise_query_layer/20260616T131752Z-3e5b1d
[agent_runner] [dry-run] no 'origin' remote; manual push: git push -u origin agent/optimise_query_layer/20260616T131752Z-3e5b1d
```

## The sample workload

### Billing — `POST /payments`

`harness/example/src/billing/routes.py` exposes a framework-agnostic handler:

```python
from routes import create_payment

status, body = create_payment({"amount": 1000, "currency": "USD", "user_id": "u_1"})
# -> 201, {"transaction_id": "txn_...", "amount": 1000, "currency": "USD",
#          "user_id": "u_1", "status": "created"}
```

Contract (full details in `harness/example/docs/API_SCHEMA.md`):

- `amount` — positive integer in **minor units** (e.g. cents).
- `currency` — one of `USD`, `EUR`, `GBP`, `ILS`.
- `user_id` — non-empty string.
- Invalid/incomplete input never raises; it returns `400` with `{"error": ...}`.
- `transaction_id` is unique per call and prefixed with `txn_`.

### Query layer — N+1 vs. batched

`harness/example/src/db/queries.py` contrasts `fetch_users_n_plus_one` (one query per id) with
`fetch_users_batched` (a single query). Both return the same rows in request
order; the batched form is the optimisation target.
