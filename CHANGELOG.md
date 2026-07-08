# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Adopt the Open Knowledge Format (OKF) as the harness info/memory layer.
- Community-health files: `CONTRIBUTORS`, `CHANGELOG.md`, issue templates, and a pull request template.

### Changed
- Resync the root landing README with the harness docs.
- Refactor the orchestrator: split `agent_runner.py` into focused `runner_*`
  modules (`runner_core`, `runner_llm`, `runner_states`, `runner_recovery`,
  `runner_containment`, `runner_reconcile`, `runner_drive`, `runner_cli`) with
  `agent_runner.py` kept as a thin re-exporting facade (public surface and
  entry points unchanged).

### Security
- Remediate red-team findings across security, leases, honesty, and rollback paths.
- Harden reconcile, leases, commit-env, win32 process kill, and ledger validation.
- Flag obfuscated git-bypass flags in the command guard.
- Close the command-guard global-flag gap and make contract binding server-authoritative.

## [0.1.0] - 2026

### Added
- Initial release of the policy-enforcing workflow harness that keeps LLM coding agents on rails.

[Unreleased]: https://github.com/yuvhaim-gif/dev_harness/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yuvhaim-gif/dev_harness/releases/tag/v0.1.0
