# Containment defences, operations & threat model

> **Relevant source:** [`../runner_containment.py`](../runner_containment.py),
> [`../ci_enforce.py`](../ci_enforce.py),
> [`../git_blob.py`](../git_blob.py),
> [`../forensic.py`](../forensic.py),
> [`../runner_cli.py`](../runner_cli.py),
> [`../journal.py`](../journal.py),
> the CI workflow [`../../.github/workflows/harness-ci.yml`](../../.github/workflows/harness-ci.yml).

## Containment defences

The pre-commit hooks are advisory — an agent that spawns its own git can skip
them. The two gates below are **authoritative** because they inspect *committed
history* instead of trusting a hook to have fired, so the usual
`git -c core.hooksPath=…` / plumbing bypasses do not defeat them.

### Post-hoc containment gate

After the mutation/repair phase and before Reconcile, the orchestrator runs
`_containment_breach()`: it inspects only **committed** state on `base..HEAD`
(the history a push would publish, so benign uncommitted scratch files are
ignored) and flags four things —

- a committed path outside the task's computed allowlist (coordination paths
  excepted **only when the committed content is a well-formed lease/journal
  payload** — a smuggled `.py` or unknown-shaped JSON under the exempt prefix is
  flagged as an *invalid coordination payload*),
- a committed **symlink or gitlink** (any non-regular tree entry — mode `120000`
  or `160000` on `base..HEAD`), even when its path is itself allowlisted —
  catching an allowed path aliased onto a locked file or turned into an
  out-of-band submodule pointer, and
- an **out-of-band commit** the orchestrator did not author itself (i.e. a SHA
  not in its `runner_commits` set — the signature of a hook-bypassed commit), and
- an **OKF info-layer violation** — a committed `spec_doc` whose OKF frontmatter
  was stripped or broken (e.g. a contract that lost its `type`), re-validated
  from the committed blob so a `validate-okf` hook that was skipped cannot let it
  through.

Any one is a breach: the run logs `CONTAINMENT BREACH`, writes a forensic
report, hard-rolls-back, journals `escalated`, and exits **4**. This is the gate
that makes the `core.hooksPath` bypass moot on the agent's own machine.

### Server-side CI re-check

`harness/ci_enforce.py` re-applies the *same* policy from a trusted runner the
agent cannot influence (wired up in `.github/workflows/harness-ci.yml`):

1. the hashed contract manifest must still verify (content-based, bypass-proof),
2. every declared `spec_doc` must still be **OKF-conformant** (the same
   `okf.verify()` the local `validate-okf` hook runs, re-checked here immediately
   after the manifest and ignoring `SKIP_AGENT_HARNESS`),
3. every file changed on an `agent/<task_id>/…` branch (computed over the
   aggregate `base...head` diff) must fall inside that task's allowlist
   (coordination paths excepted **only when well-formed** — this is the layer
   with *no* SHA-based out-of-band backstop, so a branch pushed **directly** to
   the runner that smuggles an arbitrary file under `.harness/journal/` or
   `.harness/leases/` is rejected right here as an *invalid coordination
   payload*), and
4. no change on that branch may introduce a **symlink or gitlink** — a `--raw`
   diff over the same range rejects any entry whose resulting mode is non-regular
   (`120000` symlink or `160000` gitlink), so the alias-to-a-locked-file and
   out-of-band-submodule bypasses are blocked remotely too, and
5. a contract changed on an agent branch must carry a change to at least one of
   that task's bound `contract_tests` — the same binding the local
   `enforce_contract_binding` hook applies, re-checked here so it survives a
   branch pushed without the local hook (this runner ignores `SKIP_AGENT_HARNESS`).

The task id is resolved from a **trusted** source first — an explicit `--task`
or the CI-injected `AGENT_TASK_ID` — because the branch *name* is agent-chosen
and only ever a fallback for *locating* the task, never for deciding whether the
file-scope check applies. If no task can be resolved the re-check **fails
closed** (a mislabeled branch cannot skip-and-pass); a genuine human PR
opts out explicitly by setting the trusted, workflow-set `HARNESS_NON_AGENT_OK=1`
(which the agent cannot set), after which the branch gets only the manifest check
and its file scope is the reviewer's responsibility. The base ref is resolved
before diffing — a bare `GITHUB_BASE_REF` (e.g. `main`) that does not exist in a
shallow/detached checkout falls back to `origin/<name>` (fetched if needed), so
the diff range is never silently empty. Run it locally with
`python harness/ci_enforce.py --base <ref> --head <ref> [--task <id>]`.

> **Where this gate is authoritative — and where it is not.** The file-scope
> re-check is authoritative on the **`pull_request`** event, which runs *before*
> a merge on the PR branch's real `base...head` diff. It is **advisory on a
> `push` to `main`**: after checkout the pushed commit *is* `main` *and*
> `origin/main`, so `base...head` is empty and there is nothing to re-check — the
> workflow sets `HARNESS_NON_AGENT_OK=1` on push only to keep that (necessarily
> empty) post-merge job green. `ci_enforce.py` detects the push via
> `GITHUB_EVENT_NAME` and prints an explicit **`WARN`** naming the branch-
> protection prerequisite rather than a misleading "human-authored" skip.
> Consequently the guarantee that *nothing outside a task's allowlist reaches
> `main`* holds **only if GitHub branch protection** forbids direct pushes to
> `main` and requires this `harness-ci` check to pass first — otherwise a change
> pushed straight to `main` (a leaked credential, or an agent with plain `git`)
> never passes through any PR-time re-check. Branch protection is a one-time
> repository setting the harness cannot script for you; see
> [Required GitHub branch protection](setup-and-usage.md).

---

## Operations & diagnostics

### Forensic post-mortem diagnostics

When a run is escalated, financially aborted, or crashes, `harness/forensic.py`
writes `.harness/logs/FAILED_AGENT_RUN.md` and prints a terminal status badge.
The report has four sections: (1) allowed scope vs. paths actually modified
(the containment proof), (2) terminal error codes, the condensed failing
assertions, and git policy warnings, (3) a chronological step log with token
consumption and cost per attempt, and (4) confirmation that the local working
tree was safely rolled back. The per-attempt costs in section 3 are **genuinely
per-attempt**: each Enforce attempt is paired with the *i*-th autorepair LLM
step it triggered (the final, cap-exceeding attempt has no following step and
shows zero).

The scope evidence in sections 1–3 is compiled from the working tree **before**
the rollback runs (so the containment proof is not erased by the `git reset
--hard`), while the **Rollback Verification** in section 4 reflects the *actual*
post-rollback outcome: the report is built before the rollback and emitted after
it, so the verdict (`CONFIRMED` / `NOT CONFIRMED`) is the real post-rollback
result.

**The post-mortem also joins the OKF memory layer.** Alongside the human-readable
`FAILED_AGENT_RUN.md`, the same report is written as an OKF concept document
(`type: Postmortem`, with frontmatter + body) under `.harness/logs/postmortems/`,
and a one-line, dated, newest-first summary is appended to `.harness/logs/log.md`
— an OKF reserved history file. These artifacts live under the gitignored
`.harness/logs/` tree, so they **survive the rollback** (the sweep preserves
`.harness/logs/`) and give the next agent a durable, format-consistent record of
why the previous run failed.

**Rollback also cleans `.harness/`.** The hard reset plus untracked-file sweep
removes agent-created junk *including* anything the LLM wrote under `.harness/`
(a stray `.py`, malformed JSON), while deliberately **preserving the harness's
own artifacts** there — the forensic logs (`.harness/logs/`), the telemetry sink
(`.harness/telemetry/`), the hashed manifest (`.harness/contracts.lock`), and
*well-formed* lease/journal payloads. Junk the LLM wrote under `.harness/` is
therefore not left behind by a "pristine" rollback.

**Rollback fails loud when it cannot leave the work branch.** The final
`git checkout` back to the original branch is retried once (a lingering
GitPython/Windows file handle can transiently pin the work tree); if it still
fails, `rollback_ok` is set `False`, section 4 reports **NOT CONFIRMED**, and an
`ERROR` line names the stranded branch with the manual-recovery command instead
of failing silently. Containment (**exit 4**) is unaffected — that verdict is
derived from *committed* state, not from where the local `HEAD` ends up.

### Diagnostics (`--doctor`)

The coordination layer has several moving parts (a hashed manifest, TTL'd
leases, a handover journal, a shared git ref) and, when one misbehaves,
diagnosing it by hand means git archaeology. `python -m harness --doctor`
runs a single read-only health pass and prints, in one place:

- the repo path and whether an `origin` remote / minimal mode is in effect,
- the **contract manifest** verification result (corrupt or drifted ⇒ non-zero
  exit),
- the **OKF info layer** result — whether every declared `spec_doc` is an
  OKF-conformant concept (a stripped `type` or a contract `timestamp` ⇒ non-zero
  exit),
- every local **lease** with its `ACTIVE` / `expired (reclaimable)` state, owner,
  and branch (and a `PROBLEM` line for an unreadable one),
- any **unresolved handover journals** awaiting the next agent, plus a
  **`journal files: N committed (M unresolved)`** count so journal growth is
  visible at a glance (see [Journal growth & cleanup](#journal-growth--cleanup)),
- whether the **shared state ref** resolves, and
- whether the root **README** is still the harness template (a soft warning,
  cleared by replacing it or running `--init`).

It performs no mutation and starts no run; it is safe to call at any time and
returns non-zero only on a hard problem (corrupt/drifted manifest, unreadable
lease) so it can gate CI.

### Minimal mode

The shared `harness-state` ref is the most failure-prone subsystem (push races,
ref divergence, shallow clones). If you run a **single agent on one clone** you
do not need it. Set `AGENT_MINIMAL=1` (or `AGENT_DISABLE_STATE_SYNC=1`) to keep
the full local file-lock + contract guarantee while skipping the cross-clone
git-plumbing entirely — leases and journals are still written locally, they are
just not mirrored to the shared ref.

### Journal growth & cleanup

Handover records under `.harness/journal/` are **committed** so an unresolved
session can be recovered from any clone (and, with the shared state ref, across
clones). This is intentional, but it means the directory **accumulates over
time**; `--doctor` surfaces the running total (`journal files: N committed
(M unresolved)`) so the growth never goes unnoticed.

The harness does **not** prune journals automatically. Deleting committed
journals dirties the working tree, and `initialize` refuses to start on a dirty
tree — an auto-pruner would therefore block the next run. Cleanup is a
deliberate **manual** operation: remove old, *resolved* journals and commit the
deletions yourself so the tree is clean for the next run, e.g.

```bash
git rm .harness/journal/<old-resolved-branch>.json
git commit -m "chore: prune resolved handover journals"
```

Keep any journal whose outcome is still unresolved (`escalated` / `error` /
`stale`) — those are exactly the records the next agent recovers.

Un-pruned resolved journals are harmless to correctness — recovery filters by
task and unresolved outcome, so stale files are ignored; the only cost is
repository size.

**Work branches.** A rolled-back or escalated run only leaves its
`agent/<task>/…` branch behind when it cannot mirror the handover journal off
that branch. With the **shared state ref active** (an `origin` is configured and
minimal mode is off) the journal is published to the ref, so rollback
**force-deletes the work branch** — failed runs do not accumulate orphan
`agent/*` refs. Before the branch is deleted the harness **snapshots the agent's
attempted diff**: the full patch is written to `.harness/logs/<branch>.patch` and
the dropped tip SHA plus a diffstat are recorded in `FAILED_AGENT_RUN.md`. Because
that record is plain text under the rollback-surviving forensic tree, it is
**immune to `git gc` on that machine** — you can inspect exactly what the agent
tried (`git apply --stat .harness/logs/<branch>.patch`) long after the ref is
gone, without racing your host's next prune. Note the durability boundary: like
**every** `.harness/logs/` artifact (the report, OKF postmortems, `log.md`, and
this patch), it is a **local-disk** record — `.harness/logs/` is gitignored, so
it is **not committed, not pushed, and not mirrored to the shared state ref**.
Cross-clone / fresh-checkout recovery is provided by the **handover journal**,
which *is* published to the shared ref; the patch is a local forensic convenience
layered on top, not a replacement for it. In **minimal mode or without an `origin`** the journal lives
*only* on the work branch, so the branch is deliberately **retained** (its name
is logged) as the sole local record; prune it manually once you have inspected
it:

```bash
git branch -D agent/<task>/<stamp>
```

---

## Threat model & failure modes

**What this framework is.** A *policy-enforcement* layer that keeps a cooperative
automation honest and contains an *erratic* one. It is **not** a sandbox: it does
not constrain what the `AGENT_LLM_CMD` process can do to the filesystem, network,
or environment. Its guarantee is narrower and verifiable — *nothing outside a
task's declared allowlist reaches a reviewed, pushed branch* — and it is enforced
in **defence-in-depth layers**, deliberately so that no single component is the
sole line of defence:

| Layer | Where it runs | Bypassable by the agent? | Role |
|-------|---------------|--------------------------|------|
| Command guard | before the LLM seam | yes (it only flags/strips) | raise the cost of an *accidental* bypass **baked into the `AGENT_LLM_CMD` launch string** (it never sees the agent's *runtime* commands); penalise evasion (including a git commit/push, or the bypass flag itself, hidden behind command substitution / backticks / shell variables, combined short flags like `-nm`, dashed builtins like `git-commit`, alias indirection, or buried inside a `sh -c` / `bash -lc` / `cmd /c` / `python -c` / `perl -e` script) |
| Pre-commit lock + contract hooks | agent's machine | yes (`core.hooksPath`, plumbing, or dropping `AGENT_TASK_ID` / setting `SKIP_AGENT_HARNESS` in the agent's own git) | fast, local first line; transparent to humans |
| **Post-hoc containment gate** | orchestrator, post-commit | no (inspects committed history) | authoritative local stop — **exit 4** |
| **Server-side CI re-check** | trusted runner | no | authoritative remote stop before merge |

Because the last two layers inspect *committed history* rather than trusting the
hook to have fired, the well-known `git -c core.hooksPath=…` / `commit-tree`
bypasses do not defeat them. The containment gate also **fails closed**: if the
`base..HEAD` inspection cannot run at all (e.g. the agent deletes the base
commit's objects to make `git diff` error), the un-runnable check is treated as
a breach — **exit 4** — rather than a clean pass, mirroring the fail-safe used by
`ci_enforce.py` and the strict staleness guard.

### Known failure modes & how they are handled

<details>
<summary>Full failure-mode / mitigation matrix (click to expand)</summary>

| Failure mode | Symptom | Mitigation |
|--------------|---------|------------|
| Stale / orphaned lease | a crashed agent leaves a lease behind | TTL (3600s) makes it reclaimable; `--doctor` shows `expired`; rollback releases it |
| Lease reclaim race | two agents both read one expired lease as free | reclaim is serialised behind an atomic `os.mkdir` mutex; the winner re-reads under the lock, so **exactly one** agent wins. Taking over a *stale* mutex left by a crashed reclaimer uses an atomic `os.rename` (not `rmdir`+`mkdir`, two syscalls that would let a second racer clobber the winner's fresh lock) plus a second age check, so a lock recreated between the mtime read and the steal is restored, never double-held |
| Corrupt / adversarial lease TTL | `ttl_seconds` is non-numeric in a lease file | `is_active()` treats it as **inactive (reclaimable)**, so a malformed lease never crashes the coordination check with an uncaught `ValueError` |
| Poisoned coordination state | a payload smuggled under the allowlist-exempt `.harness/leases/` or `.harness/journal/` (e.g. a `.py`, nested path, or unknown-shaped JSON) — a persistent prompt-injection vector, since journal content feeds the next agent's prompt | the exemption is **content-aware** (`is_valid_coordination_payload`): the pre-commit hook (exit 1), the post-hoc containment gate (exit 4), **and** the server-side CI re-check (the layer with no SHA backstop, so it stops a *directly pushed* branch) all reject anything that is not a flat well-formed lease/journal `*.json`; the immutable prompt rules additionally tag handover/journal text as **untrusted data** |
| Corrupt `contracts.lock` | manifest file is not valid JSON | `CorruptLockError` ⇒ a clear "run `--update`" message, never a traceback; surfaced by `--doctor` and CI |
| Malicious / malformed task id | a task key or `--release`/`--task` value like `../contracts` or `../../etc/x` is fused into the lease/journal file path and the work-branch name, and could traverse out of `.harness/` | `leases.is_valid_task_id` enforces a conservative slug (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `..`) and **fails closed** at every entry point: `lease_path` raises, `_parse_task` and `release_lease` reject before any filesystem write, and the `validate-agents-ledger` pre-commit hook rejects an unsafe ledger *key* (not just its values) |
| Shared-ref push race | concurrent `publish_files` non-fast-forward | bounded exponential backoff + jitter retry; `False` return is logged as a warning, never swallowed |
| Poisoned shared-ref lease | any principal with write access to `origin` pushes a fake "live" lease to the `harness-state` ref to strand another agent's task | bounded by the per-clone lease **TTL** + the pre-push **staleness guard** + content-aware `is_valid_coordination_payload` (a smuggled non-lease/non-journal blob is rejected at the CI re-check); it is a coordination convenience, not a consensus system — for stronger guarantees push to a protected ref or run one clone per agent (`AGENT_MINIMAL=1`). Signed lease payloads are a roadmap item |
| Shallow clone in CI | base-commit objects absent ⇒ staleness silently passes | auto `fetch --unshallow`; with `AGENT_STALENESS_STRICT=1`, an unresolvable ref **fails safe** |
| Hook bypass (`core.hooksPath`, plumbing) | a commit lands without the lock hook firing | guard flags it; post-hoc containment gate aborts (exit 4); CI re-check blocks the merge |
| Hook bypass (env var) | the local hooks gate on `AGENT_TASK_ID` and skip on the human-only `SKIP_AGENT_HARNESS`, so the agent's own git subprocess can run `env -u AGENT_TASK_ID git commit …` (or set `SKIP_AGENT_HARNESS=1` itself) to no-op the *local* hook — same severity class as `core.hooksPath` | the orchestrator's own commits always re-assert `AGENT_TASK_ID` and drop `SKIP_AGENT_HARNESS`; any commit the agent makes out of band still lands on `base..HEAD`, so the post-hoc containment gate catches it by SHA (`_unexpected_commits`, exit 4) and the CI re-check blocks the merge |
| Containment check cannot run | the agent tampers with git so the `base..HEAD` inspection itself errors (e.g. deletes the base commit's loose object) — a naive `except: return []` would read as "no violations" | the probes **fail closed**: an un-runnable check raises `ContainmentCheckError`, which the gate reports as a breach (exit 4) instead of a clean pass; a genuine deletion of a committed path still reads as benign (`None`), so this adds no false positives |
| Obfuscated bypass flag | `--no-verify`/`-n` hidden behind `$(...)`, backticks, `$GIT`, a bypass argument wrapped in a command substitution (`git commit -m x $(echo --no-verify)`) or routed through a shell variable (`FLAG=--no-verify; git commit … $FLAG`), a combined short flag (`-nm` == `-n -m`), a dashed builtin (`git-commit`), a `git -c alias.x=commit` indirection, or buried inside a `sh -c` / `bash -lc` / `cmd /c` / `python -c` / `perl -e` script so the stripper misses it | guard unpacks the combined-short/dashed forms and strips the `-n`, and flags what it cannot rewrite (`obfuscated git-bypass` / `indirected git-bypass` / `shell -c …` / `interpreter …` / `git alias override`), charging a `guard_penalties` hit; the post-hoc containment gate + CI re-check still catch any resulting commit |
| Out-of-allowlist commit | agent commits a file it may not touch | post-hoc containment gate (exit 4) locally; `ci_enforce.py` rejects the branch remotely |
| Branch-name allowlist dodge | agent pushes its work on a branch that does not match `agent/<task>/…` so the CI file-scope re-check finds no task | `ci_enforce.py` **fails closed** — the task id is trusted from `AGENT_TASK_ID`/`--task` first (the branch name only *locates* a task, never decides whether the check applies); an unresolved task rejects the branch unless the trusted, workflow-set `HARNESS_NON_AGENT_OK=1` opts a genuine human PR out |
| Direct push to `main` (no PR) | a leaked credential or an agent with plain `git` pushes an out-of-scope change straight to `main`, so no PR-time re-check ever runs; the push-event CI job has an empty `base...head` and cannot inspect it | **not** closeable from inside CI — the pushed commit *is* both `base` and `head`. Requires **GitHub branch protection** (forbid direct pushes to `main` + require the `harness-ci` PR check) as a deployment prerequisite; `ci_enforce.py` prints an explicit `WARN` on push events naming it, and it is documented in [setup-and-usage.md](setup-and-usage.md) and the *Honest limitations* below |
| Empty CI diff range | a bare `GITHUB_BASE_REF` (e.g. `main`) does not resolve in a shallow/detached checkout, silently emptying the diff and passing a rogue branch | `_resolve_base()` falls back bare → `origin/<name>` → shallow fetch before diffing, so the range is never silently empty |
| Stranded rollback | a lingering file handle (Windows) pins the work tree and the final `git checkout` back to the original branch fails | retried once after `gc.collect()`; on final failure `rollback_ok` flips **False**, section 4 reports **NOT CONFIRMED**, and an `ERROR` names the stranded branch + manual-recovery command (not a swallowed warning) — containment (exit 4) is unaffected, being derived from *committed* state |
| Symlink / gitlink lock bypass | an allowlisted path is flipped to a symlink (mode `120000`) aliasing a locked file, or to a gitlink (mode `160000`) smuggling out-of-band submodule content | every lock layer rejects any non-regular mode (`symlink_paths()` flags anything that is not `100644`/`100755`/deletion): pre-commit exit 1, containment gate exit 4, CI re-check fail |
| OKF info-layer corruption | an evolve-mode edit strips a spec_doc's `type` or adds a volatile `timestamp` to a contract, degrading the durable knowledge layer | `okf.verify()` runs at four layers: `validate-okf` pre-commit (exit 1), post-hoc containment gate (exit 4, re-validated from the committed blob), CI re-check, and `--doctor` |
| Budget breach | token/USD ceiling exceeded mid-run | immediate financial abort, rollback, forensic report, exit 3. **Caveat:** the token/cost figures are **self-reported** by `AGENT_LLM_CMD` via `usage.json`; a backend that reports zero (malicious or broken) never trips `MAX_TOTAL_TOKENS` / `MAX_RUN_COST_USD`. Budgeting is an accounting aid over a *trusted input*, not a metered control — meter spend at your provider/proxy if the backend is untrusted |
| Coordination layer is overkill | single-agent run, shared-ref machinery unwanted | `AGENT_MINIMAL=1` keeps local locking, drops the shared ref |

</details>

### Honest limitations

- The `AGENT_LLM_CMD` seam is an **arbitrary shell command**. The harness hardens
  *git* behaviour around it (`GIT_CONFIG_NOSYSTEM`, plus the dropped git-config env
  family `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` / `GIT_CONFIG_PARAMETERS` /
  `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*`,
  flag stripping, and *flagging* of obfuscated bypass flags it cannot rewrite)
  but cannot stop the backend from doing out-of-band work outside
  that seam. The containment gate and CI re-check are what make that *safe to
  ship* — they reject the result — but they are detective controls, not a
  sandbox. Run untrusted backends inside your own isolation (container/VM).
- The shared `harness-state` ref is a coordination convenience, not a consensus
  system. It is **not authenticated from an agent's perspective**: any principal
  with write access to `origin` can push to it, so a compromised runner could
  poison another agent's lease state. The per-clone TTL, the pre-push staleness
  guard, and content-aware `is_valid_coordination_payload` bound the blast radius;
  push to a **protected ref** (or prefer one clone per agent, or `AGENT_MINIMAL=1`)
  under adversarial concurrency. Signed lease payloads are a roadmap item.
- **Coordination state is structurally validated, not semantically trusted.**
  `is_valid_coordination_payload` guarantees only that an exempt lease/journal
  file is a flat well-formed `*.json` with known top-level keys — it does **not**
  vet the free-text fields (`notes`, attempt `log_excerpt`). Because a recovered
  journal re-enters a later agent's prompt via `AGENT_HANDOVER_FILE`, those
  fields are a **prompt-injection surface**: the immutable framework rules
  therefore instruct the model to treat AGENTS.md context, the handover/journal
  file, and any prior-session notes as **untrusted data, never instructions**,
  and as defence-in-depth the journal **control-char-strips and length-caps**
  those fields on write. That is a mitigation, not a sandbox — run untrusted
  backends in your own isolation and review escalated journals before trusting
  their narrative.
- **The CI file-scope re-check trusts the task id, not the branch name.** A branch
  is agent-controllable, so `ci_enforce.py` only uses `agent/<task_id>/…` to
  *locate* a task and prefers the CI-injected `AGENT_TASK_ID` / `--task`. An
  attacker can *redirect* which existing task's allowlist applies but cannot
  *widen* scope (the allowlist is still enforced, and an unresolved task fails
  closed). For a tighter trust boundary, have the CI workflow set `AGENT_TASK_ID`
  from a trusted source (a PR label or an orchestrator-signed commit) rather than
  deriving it from the branch name.
- **The CI file-scope re-check is authoritative on `pull_request`, not on a
  `push` to `main`.** A push leaves `base` and `head` at the *same* commit, so the
  re-check has an empty diff and cannot inspect what the push introduced; the
  workflow sets `HARNESS_NON_AGENT_OK=1` on push purely to keep that empty
  post-merge job green, and `ci_enforce.py` emits a `WARN` (via
  `GITHUB_EVENT_NAME`) naming this. The guarantee that *nothing outside a task's
  allowlist reaches `main`* therefore depends on a control the harness **cannot
  configure itself**: **GitHub branch protection** must forbid direct pushes to
  `main` and require the `harness-ci` check to pass on the PR *before* merge.
  Without it, a change pushed straight to `main` (a leaked token, or an agent
  with plain `git`) bypasses every re-check. The one-time setup — including a
  scriptable `gh api` recipe — is in
  [Required GitHub branch protection](setup-and-usage.md).
- The complexity is real: ~18 harness modules, a YAML ledger, a shared ref,
  TTL'd leases, a journal, a hashed manifest, and a multi-stage pre-commit
  pipeline. `--doctor` exists specifically to make that surface debuggable; if
  you do not need cross-agent coordination, minimal mode collapses most of it.
