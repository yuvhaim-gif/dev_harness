# Cross-agent coordination

> **Relevant source:** [`../leases.py`](../leases.py),
> [`../journal.py`](../journal.py),
> [`../state_sync.py`](../state_sync.py),
> [`../staleness.py`](../staleness.py),
> [`../lock_policy.py`](../lock_policy.py).

When two or more agents may run the same harness in parallel (or on different
clones), four lightweight mechanisms keep them from colliding or losing
context:

- **Leases** (`harness/leases.py`) — Isolate writes
  `.harness/leases/<task_id>.json` with the owning `agent_id`, branch, base
  commit, declared targets, and a 3600s TTL. A **fresh** claim is created with
  `O_CREAT | O_EXCL` so two agents that both read "absent" cannot each believe
  they won. **Reclaiming an expired lease** is serialised behind a short-lived
  atomic mutex (`os.mkdir` on a sibling `.lock` directory, stolen if older than
  30s): the winner re-reads under the lock before committing the claim, so two
  agents that both saw the lease as expired can no longer both `os.replace` their
  own copy and both return success — exactly one wins (the prior read→replace
  reclaim had a real TOCTOU race here). The lease itself is still written
  **atomically** (sibling temp file `os.replace`d into place, with a bounded
  retry to ride out the Windows sharing window when another agent has it open for
  reading). A corrupt or adversarial lease with a non-numeric `ttl_seconds` is
  treated as **inactive (reclaimable)** rather than crashing the check the whole
  coordination layer depends on. Reconcile (and rollback) releases the lease.
- **Handover journal** (`harness/journal.py`) — every session writes an
  append-only JSON record to `.harness/journal/` containing each attempt's
  state, status, and hook-log excerpt, plus a terminal outcome
  (`in_progress` / `pushed` / `local` / `stale` / `escalated` / `error`).
  Initialize calls `latest_unresolved()` for the task and surfaces the
  previous session's context to the LLM via `AGENT_HANDOVER_FILE`, so a
  rolled-back or escalated run is never lost. Because this content re-enters a
  later agent's prompt, it is treated as **untrusted data** — see the
  [coordination-state injection](containment-and-diagnostics.md#threat-model--failure-modes) note.
- **Coordination-payload validation** (`lock_policy.is_valid_coordination_payload`)
  — `.harness/leases/` and `.harness/journal/` are exempt from the allowlist
  because the orchestrator, not the LLM, writes them. That exemption is
  **content-aware**, not path-blind: an exempt path is accepted only when it is a
  flat `*.json` object directly under the coordination dir whose top-level keys
  are a subset of the known lease/journal schema. An arbitrary file (a stray
  `.py`), a nested path, or unknown-shaped JSON smuggled under the prefix is
  rejected by **every** layer that honours the exemption (pre-commit hook,
  post-hoc containment gate, and the server-side CI re-check).
- **Shared state ref** (`harness/state_sync.py`) — leases and journal
  entries committed only on an abandoned work branch are invisible to a fresh
  clone of `main`. The orchestrator mirrors them onto a dedicated ref
  (`AGENT_STATE_REF`, default `harness-state`) via pure git plumbing
  (`read-tree` / `update-index` / `commit-tree` / `push`). The working tree
  and current branch are untouched, so it is safe to call from any state. A
  fresh clone can read coordination state directly off the ref. Pushes race, so
  `publish_files()` retries a non-fast-forward with **bounded exponential
  backoff + jitter** and returns `False` when every attempt is exhausted —
  callers treat that as a real coordination failure and **log a warning** rather
  than silently swallowing it. This is also the layer most exposed to git
  edge-cases; if you do not need cross-clone coordination, run in
  [minimal mode](containment-and-diagnostics.md#minimal-mode) to switch it off entirely.
- **Optimistic staleness guard** (`harness/staleness.py`) — before
  pushing, Reconcile diffs the **critical paths** (contracts, spec docs,
  `locked_files`, the task's own **targets**, the always-locked set, and
  `.harness/contracts.lock`) at the agent's base commit against
  `AGENT_SHARED_REF` (default `origin/main`). Including the targets means that if
  two agents race the lease for an isolated-mode task, the loser is still caught
  here when a target it built on has since moved on the shared ref.
  Any difference means the shared truth moved while the agent worked; the run
  journals `stale`, refuses the push, and exits 1, handing the conflict to
  the next agent rather than landing work built on a superseded contract. On a
  **shallow clone** (common in CI) the base-commit objects may be absent, which
  would make the comparison silently pass; the guard deepens the clone first
  (`fetch --unshallow`), and when the shared ref still cannot be resolved it
  honours `AGENT_STALENESS_STRICT=1` by **failing safe** (refusing the push)
  instead of skipping the check.
