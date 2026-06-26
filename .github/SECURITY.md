# Security Policy

The Agent Workflow Harness is a policy-enforcement and containment framework, so
the integrity of its enforcement gates (file-locking, contract binding,
post-hoc containment, and the server-side CI re-check) is treated as a security
boundary. We welcome reports of any flaw that lets an agent escape those gates.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |

Only the latest released `0.1.x` line receives security fixes during the
pre-1.0 phase.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub's built-in workflow:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability** (GitHub Private Vulnerability Reporting).
3. Include a description, affected version/commit, reproduction steps, and the
   impact (for this project, especially *which enforcement gate is bypassed*).

If private reporting is unavailable, contact the maintainer listed in the
repository's `LICENSE` / commit history instead of filing a public issue.

## What to expect

- **Acknowledgement** within 5 business days.
- An initial assessment and severity triage shortly after.
- Coordinated disclosure: we will agree on a timeline and credit you in the
  release notes unless you prefer to remain anonymous.

## Scope

In scope: any way for an LLM/automated agent driven by `python -m harness` to
commit, push, or land changes outside its declared allowlist, alter a pinned
contract without detection, evade the pre-commit or CI gates, or defeat the
lease / staleness / budget controls.

Out of scope: vulnerabilities in third-party dependencies (report those
upstream) and issues that require an already-trusted human operator to act
maliciously.
