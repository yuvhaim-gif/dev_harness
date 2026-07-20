# Contributing

Thanks for your interest in improving the Agent Workflow Harness. This is a
containment framework, so contributions are reviewed with an emphasis on never
weakening an enforcement gate.

The harness is itself coded with LLM tools and verified by **independent** LLM
agents — the agent that reviews and tests a change is a separate one from the
agent that authored it. Whether a change is written by a human or an agent, it
must pass the deterministic gates below and an independent review before merge.

## Getting started

```bash
python -m pip install --upgrade pip
pip install .[dev]
```

## Before you open a pull request

Run the same checks the CI runner enforces (`.github/workflows/harness-ci.yml`):

```bash
ruff check .
ruff format --check .
mypy --strict --ignore-missing-imports harness
pytest -q
```

All four must pass. New behaviour should ship with a test under
`harness/tests/` (framework self-tests) or `harness/example/tests/` (sample
workload).

## Commit and PR conventions

- Use conventional-commit prefixes (`feat`, `fix`, `perf`, `docs`, `chore`,
  `test`).
- Keep each PR focused on a single concern.
- If you change an enforcement gate, explain in the PR description why the
  guarantee is preserved (or strengthened) and add a regression test.
- Do not disable or loosen a gate to make a test pass.

## Reporting bugs and requesting features

Open a GitHub issue with clear reproduction steps or a concrete use case.
For **security-sensitive** problems, follow `SECURITY.md` instead of opening a
public issue.

## Code of conduct

By participating you agree to abide by `CODE_OF_CONDUCT.md`.
