<!-- HARNESS_TEMPLATE_README
This landing page is the harness's own documentation, shown on the repo's
GitHub page. If you are adopting the harness for your own project, replace
this file with your project's README -- either delete it and write your own,
or run `python -m harness --init`, which stamps a project stub and an empty
AGENTS.md ledger. `python -m harness --doctor` warns while this marker is
present. The full framework docs always live under `harness/README.md`.
-->

# Agent Workflow Harness

A hardened framework that keeps automated / LLM coding agents **on the rails**
using a strict five-state loop and programmatic **file-locking** enforced at
commit time. An agent may only touch the files a task explicitly declares;
everything else is locked, and the orchestrator never leaves the repository in
a half-broken state.

> **How this harness is built and verified.** The harness is itself developed
> with LLM coding tools and verified by **independent** LLM agents — a separate
> agent from the one that produced a change reviews and tests it — so the
> framework is dogfooded by the very kind of automated agents it is designed to
> keep on the rails.

## The problem it solves

**The problem.** An autonomous / LLM coding agent turned loose on your
repository can edit files it was never meant to touch, silently change a stable
API, or leave the working tree half-broken when it fails mid-task.

**The solution.** You declare, *per task*, exactly which files an agent may
change. Everything else is locked. The rules are enforced three times — when the
agent commits, again on the committed history right after, and a third time in
CI on every pull request — so nothing out of scope reaches `main` through the
review flow.

> **The guarantee:** *nothing outside a task's declared allowlist reaches a
> reviewed, pushed branch.*
>
> The CI re-check is authoritative on the pull request, *before* merge; it is
> advisory on a direct `push` to `main`. Closing that requires a one-time
> **branch-protection** setting so a direct push cannot skip the PR check — see
> [Required GitHub branch protection](harness/docs/setup-and-usage.md).

**Who this is for**

- **Use it** if you run automated / LLM agents against a real repository and
  need hard, verifiable limits on what they may change.
- **You may not need it** if you only run a single trusted agent on a throwaway
  branch — [minimal mode](harness/docs/containment-and-diagnostics.md#minimal-mode) collapses most of the machinery.
- **It is not a sandbox.** It constrains what reaches a branch, not what the
  agent process can do to your machine — run untrusted backends in a
  container / VM. In particular the command guard only inspects the configured
  `AGENT_LLM_CMD` launch string, never the git commands the agent runs at
  runtime; the post-commit containment gate is the boundary that catches those.

## Quick start

```bash
git init -b main                 # the project assumes a `main` branch
python -m venv .venv             # create a virtualenv
.venv\Scripts\activate           # POSIX: source .venv/bin/activate
pip install .[dev]               # runtime + ruff / mypy / pytest
pre-commit install               # register the local commit gates
python -m harness --init         # stamp your README + an empty AGENTS.md
python -m harness --doctor       # one-pass health check
```

<details>
<summary>What each step does</summary>

- **`git init -b main`** — the harness expects the `main` branch; don't rely on a
  bare `git init`.
- **virtualenv + `pip install .[dev]`** — installs the runtime deps plus the
  lint/type/test tools the gates use.
- **`pre-commit install`** — wires the file-lock, contract, and OKF hooks into
  local git.
- **`python -m harness --init`** — replaces the template README with a project
  stub and seeds an empty `AGENTS.md` (`--example` reproduces the demo ledger).
- **`python -m harness --doctor`** — reports the health of every coordination
  subsystem; safe to run any time.

</details>

See [Setup, usage & the sample workload](harness/docs/setup-and-usage.md) for the
full walkthrough.

## How it works in 5 steps

1. **Declare** a task's file scope in `AGENTS.md` — its `targets`, `tests`, and
   `spec_docs`.
2. **Isolate** — the harness claims a TTL'd lease and cuts a dedicated work
   branch.
3. **Mutate** — it invokes your agent (`AGENT_LLM_CMD`) to edit *only* the
   allowed files.
4. **Enforce** — it stages just the allowlist and commits behind the lock /
   contract hooks, auto-repairing test failures within a budget.
5. **Reconcile** — it re-inspects the committed history, guards against a moved
   base branch, then pushes and opens a PR.

The full state machine — including the abort, budget, and repair paths — is the
[five-state loop](harness/docs/overview.md#the-five-state-loop).

## Documentation

The full framework reference lives under [`harness/README.md`](harness/README.md)
(the documentation hub) and the focused documents in
[`harness/docs/`](harness/docs/):

| Document | Covers |
|----------|--------|
| [Overview & architecture](harness/docs/overview.md) | The problem it solves, how it works in 5 steps, key concepts, the five-state loop, and the repository layout. |
| [The ledger, lock model & enforcement](harness/docs/ledger-and-locks.md) | `AGENTS.md` task fields, the `evolve` / `isolated` allowlist model, and the pre-commit file-lock hook. |
| [The OKF information layer & contract binding](harness/docs/okf-and-contracts.md) | OKF conformance for `spec_docs` and the hash-pinned contract manifest + bound-test binding. |
| [Cross-agent coordination](harness/docs/coordination.md) | Leases, the handover journal, the shared state ref, and the staleness guard. |
| [LLM integration seam](harness/docs/llm-seam.md) | The `AGENT_LLM_CMD` seam, token/cost budgeting, log condensation, cache-ordered prompts, the command guard, and the human override. |
| [Containment defences, operations & threat model](harness/docs/containment-and-diagnostics.md) | The post-hoc containment gate, the CI re-check, forensic diagnostics, `--doctor`, minimal mode, and the threat model. |
| [Setup, usage & the sample workload](harness/docs/setup-and-usage.md) | Requirements, setup, the CLI, and the bundled sample workload. |
| [Testing, the pre-commit pipeline & design notes](harness/docs/testing-and-design.md) | The self-tests, the pre-commit pipeline, design notes, and extending the framework. |

## Key concepts

| Term | In one line |
|------|-------------|
| **Ledger (`AGENTS.md`)** | YAML file where each task declares the files an agent may touch. |
| **Allowlist** | The exact set of paths a task may change; everything else is locked. |
| **`evolve` / `isolated`** | Task modes: `evolve` may edit specs + tests + targets; `isolated` only targets. |
| **Lease** | A short-lived claim on a task so two agents never work it at once. |
| **Handover journal** | An append-only record of each run, recovered by the next agent. |
| **Contract** | A stable spec doc, hash-pinned; changing it must co-change a bound test. |
| **OKF** | [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — the frontmatter standard the `spec_docs` follow. |
| **Containment gate** | Post-commit check of committed history; aborts (**exit 4**) on any breach. |
| **Staleness guard** | Refuses to push if the shared branch moved under the agent's feet. |
| **`--doctor`** | Read-only health report across every coordination subsystem. |
