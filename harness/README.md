# Agent Workflow Harness

A hardened framework that keeps automated / LLM coding agents **on the rails**
using a strict five-state loop and programmatic **file-locking** enforced at
commit time. An agent may only touch the files a task explicitly declares;
everything else is locked, and the orchestrator never leaves the repository in
a half-broken state.

This is the framework's own documentation hub. The deep reference is split into
focused documents under [`docs/`](docs/); this page indexes them and captures the
big-picture invariants.

This documentation describes what is actually implemented in the repository:

- the **orchestrator** and its five-state loop;
- the **file-lock and contract-binding hooks**;
- the **OKF information layer** — every task's `spec_docs` is an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
  concept bundle, gated for conformance;
- the **cross-agent coordination layer** — leases, handover journal, shared
  state ref, and staleness guard;
- the **LLM-execution hardening layer** — token/cost budgeting, log
  condensation, cache-ordered repair prompts, escape-hatch command interception,
  the human override switch, and forensic post-mortem diagnostics; and
- the **out-of-band containment defences** — a post-hoc commit gate, a
  `--doctor` health report, an optional minimal mode, and a server-side CI
  re-check that holds even when the local hooks are skipped.

> **Honest threat model.** The local pre-commit hooks are the *first* line of
> defence, not the last. An LLM that spawns its own git can skip them
> (`git -c core.hooksPath=…`, low-level plumbing). The harness therefore treats
> the hooks as advisory and relies on two **authoritative** gates the agent
> cannot influence: a **post-hoc containment check** in the orchestrator
> (inspects committed history, aborts with **exit 4** on any breach) and a
> **server-side CI re-check** (`harness/ci_enforce.py`) on a trusted runner. See
> [Containment defences, operations & threat model](docs/containment-and-diagnostics.md).

## Documentation

| Document | Covers |
|----------|--------|
| [Overview & architecture](docs/overview.md) | The problem it solves, how it works in 5 steps, key concepts, the five-state loop (+ exit codes), and the repository layout. |
| [The ledger, lock model & enforcement](docs/ledger-and-locks.md) | `AGENTS.md` task fields, the `evolve` / `isolated` allowlist model, coordination-path and symlink/gitlink handling, and the pre-commit file-lock hook. |
| [The OKF information layer & contract binding](docs/okf-and-contracts.md) | OKF conformance for `spec_docs`, reserved-file rules, and the hash-pinned contract manifest + bound-test binding. |
| [Cross-agent coordination](docs/coordination.md) | Leases, the handover journal, coordination-payload validation, the shared state ref, and the optimistic staleness guard. |
| [LLM integration seam](docs/llm-seam.md) | The `AGENT_LLM_CMD` seam and its env vars, token/cost budgeting, log condensation, cache-ordered prompts, the command guard, and the human override switch. |
| [Containment defences, operations & threat model](docs/containment-and-diagnostics.md) | The post-hoc containment gate, the server-side CI re-check, forensic diagnostics, `--doctor`, minimal mode, journal cleanup, and the full threat model. |
| [Setup, usage & the sample workload](docs/setup-and-usage.md) | Requirements, setup, running the orchestrator (CLI flags + exit codes), and the bundled sample workload. |
| [Testing, the pre-commit pipeline & design notes](docs/testing-and-design.md) | The framework self-tests, the pre-commit pipeline, design/hardening notes, and how to extend the framework. |

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
