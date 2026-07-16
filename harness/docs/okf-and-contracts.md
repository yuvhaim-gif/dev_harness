# The OKF information layer & contract binding

> **Relevant source:** [`../okf.py`](../okf.py),
> [`../validate_okf.py`](../validate_okf.py),
> [`../contract_manifest.py`](../contract_manifest.py),
> [`../enforce_contract_binding.py`](../enforce_contract_binding.py),
> [`../tests/test_contracts.py`](../tests/test_contracts.py),
> the OKF bundle under [`../example/docs/`](../example/docs/),
> the manifest [`../../.harness/contracts.lock`](../../.harness/contracts.lock).

## The OKF information layer

OKF conformance is **opt-in and scoped**: it applies only to files a task
explicitly lists under `spec_docs` (of which `contracts` is a subset).
`spec_docs` is optional — a task may declare none — and every other Markdown
file in the repo is ignored by the gate. Adopting the harness does **not**
require converting existing documentation; you add a one-line `type:`
frontmatter only to the specific docs you hand an agent as its knowledge bundle.

Every task's `spec_docs` is treated as an **[Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
(OKF v0.1) concept bundle** — the durable *information/memory layer* the harness
reasons over, distinct from the machine-coordination state (leases, journal,
`contracts.lock`) which stays strict JSON. A bundle is just a directory of
markdown files (one per task's `spec_docs`), each carrying YAML frontmatter, so
the knowledge stays human-readable, git-diffable, and maximally interoperable
with any other OKF consumer.

`harness/okf.py` defines the conformance rules, deliberately **minimal** so the
corpus stays portable (OKF mandates permissive consumption):

- **Concept files** — every non-reserved `spec_doc` MUST carry a YAML frontmatter
  block with a **non-empty `type`**. That is the only hard gate on an ordinary
  concept; any other keys (`title`, `description`, `tags`, `timestamp`, …) are
  free-form and tolerated.
- **Reserved files** follow OKF's own rules and are exempt from the `type` gate:
  - **`index.md`** — the bundle root / progressive-disclosure map. Its
    frontmatter, if present, may contain **only** `okf_version`; a stray typed
    key is rejected. It may also omit frontmatter entirely.
  - **`log.md`** — the dated, newest-first history file. It is validated
    **leniently** (no frontmatter required) so an append-only log never trips the
    gate.
- **Contract concepts** additionally MUST NOT declare a volatile **`timestamp`**.
  The contract hash in `.harness/contracts.lock` is taken over the **whole file**
  (frontmatter included), so an edit-time timestamp would churn the pinned hash
  on every touch and defeat the drift check — see [Contract binding](#contract-binding).

The reserved `index.md` / `log.md` are declared as ordinary `spec_docs`, which
puts them inside the **`evolve` allowlist**: an agent (or a human) may curate the
bundle's map and history in-band, while a task in `isolated` mode cannot touch
them at all.

**Enforcement mirrors the file-lock model — advisory hook + authoritative
re-checks.** The same conformance rule is applied at four layers so a
frontmatter-stripping edit cannot reach a merged branch:

| Layer | Where | Bypassable? |
|-------|-------|-------------|
| `validate-okf` pre-commit hook (`harness/validate_okf.py`) | agent's machine | yes — honours `SKIP_AGENT_HARNESS` for human restructures |
| Post-hoc containment gate (`_okf_violations` → exit 4) | orchestrator, post-commit | no — inspects committed blobs on `base..HEAD` |
| Server-side CI re-check (`ci_enforce.py`, ignores the override) | trusted runner | no |
| `--doctor` info-layer report | on demand | n/a (read-only) |

Because the last three inspect *committed content* rather than trusting the hook
to have fired, an agent that strips the OKF frontmatter off a committed
`spec_doc` — keeping its path inside the allowlist so the file-lock gates pass it
— is still caught as an **OKF info-layer violation** (exit 4 locally, CI failure
remotely).

Forensic post-mortems also join this layer: a rejected/crashed run is written as
an OKF `Postmortem` concept plus a `log.md` entry under `.harness/logs/` — see
[Forensic post-mortem diagnostics](containment-and-diagnostics.md#forensic-post-mortem-diagnostics).

## Contract binding

A task's `contracts` are the subset of its `spec_docs` that are treated as
**stable surface area**. Their content is hash-pinned in
`.harness/contracts.lock` — the sha256 is taken over the **whole file, OKF
frontmatter included**, so a `type` rename or any frontmatter edit is itself a
contract change (which is why contract concepts may not carry a volatile
`timestamp`; see [The OKF information layer](#the-okf-information-layer)). Two
pre-commit hooks keep the binding honest:

- **`verify-contract-manifest`** (`harness/contract_manifest.py`) — for
  every contract declared anywhere in the ledger, the file's sha256 must match
  the manifest. A missing entry, a drifted hash, or a manifest entry for an
  undeclared contract is reported and aborts the commit. Run
  `python harness/contract_manifest.py --update` to record an intentional
  contract change.
- **`enforce-contract-binding`** (`harness/enforce_contract_binding.py`)
  — if a commit stages any contract file, the **same commit** must also stage
  `.harness/contracts.lock` and (when the task declares any) at least one of
  its `contract_tests`. This prevents a silent contract revision: the rules
  that pin the contract have to move with it.

`harness/tests/test_contracts.py` mirrors the manifest hook at test time, so a
drifted contract fails CI even when commits are bypassed.
