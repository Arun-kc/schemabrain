# Security Policy

Schema Brain is an MCP server that connects to production-grade databases.
We take vulnerability reports seriously. This document explains how to
report a vulnerability, what to expect in response, and what is in scope.

## Supported Versions

Schema Brain is in public alpha (`0.1.0aN` on PyPI). Only the latest
alpha and the `main` branch receive security fixes today. When 1.0 ships,
this table will list the supported minor versions.

| Version          | Supported          |
| ---------------- | ------------------ |
| `main` (HEAD)    | :white_check_mark: |
| Latest `0.1.0aN` | :white_check_mark: |
| Older alphas     | :x:                |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security reports.**

Use one of these private channels instead:

1. **GitHub Private Vulnerability Reporting (preferred):**
   <https://github.com/Arun-kc/schemabrain/security/advisories/new>
   This opens a private advisory thread visible only to project maintainers.
   It is the fastest path to a fix and a coordinated CVE if warranted.

2. **Email:** `arunkc91@gmail.com` with subject prefix `[schemabrain-security]`.

A useful report includes:
- Schema Brain version (`schemabrain --version`)
- Python version and OS
- Minimal reproduction steps
- Impact assessment in your words (what an attacker could do)
- Suggested fix, if you have one

## Response Expectations

Schema Brain is currently maintained by one person on a part-time basis,
which shapes the response targets below. These are *targets* — we'll
publish a status update on the advisory thread if real life delays
any of them.

- We aim to acknowledge receipt within **7 calendar days**.
- We aim to provide an initial assessment (in scope / out of scope,
  severity) within **14 calendar days**.
- For confirmed, in-scope vulnerabilities, we target a fix or a
  publicly tracked workaround within **90 calendar days** of
  acknowledgement. Critical-severity issues (authentication bypass,
  RCE, data-exfiltration through the MCP surface) get priority over
  the 90-day target.

If you do not hear back within 7 days, please escalate by re-sending via
the other channel.

## Coordinated Disclosure

We follow a 90-day coordinated-disclosure window from the date the report
is acknowledged. Reporters who would like credit will be acknowledged in
the release notes (CHANGELOG.md) for the fix release. Schema Brain does
not currently offer a bug bounty.

## In Scope

- The published PyPI package (`schemabrain`) on the latest alpha and the
  `main` branch
- The MCP server surface (`schemabrain serve`), including all exposed
  tools
- The `schemabrain index`, `schemabrain eval`, and `schemabrain fixture-path`
  CLI commands
- PII redaction in sample values written to the local SQLite store
- The bundled example configs and demo script under `examples/`

## Out of Scope

- Vulnerabilities in third-party dependencies should be reported to
  the upstream project. We track upstream advisories via `pip-audit`
  in CI and Dependabot weekly, so most upstream CVEs become Schema
  Brain dep-bump PRs automatically. If you believe an upstream CVE
  has a Schema-Brain-specific exploit path that the upstream patch
  alone won't close, please report it here.
- Supply-chain compromise of a build-time-only dep (e.g. a test
  framework, a CI tool) that does not materially affect the published
  PyPI wheel. Schema Brain's distributable artifacts are gated by
  `pip-audit` and the deps declared in `pyproject.toml`'s `dev` extra
  do not ship to end users.
- Self-hosted deployments of Schema Brain where the operator has
  intentionally exposed it to untrusted networks. Schema Brain is
  designed for local-only use today; hardened multi-tenant operation is
  on the roadmap, not the current product.
- Issues that require an already-compromised host or root access to
  exploit.
- DoS attacks against an MCP client through the agent's natural
  ability to issue arbitrary tool calls — agent-side rate-limiting is
  the client's responsibility today; an SLO contract for the server
  side is on the v1 roadmap.

## Security Posture Today

Schema Brain currently:

- Validates and canonicalizes Postgres URLs at the boundary, stripping
  credentials before any logging or display
- Reads connection URLs from `--url-env VARNAME` so passwords never
  appear in argv (and emits a deprecation warning when they do)
- Uses parameterized SQL throughout; identifier f-strings only assemble
  pre-quoted identifiers from SQLAlchemy's `identifier_preparer`
- Redacts PII (emails, SSNs, credit-card numbers) before sample values
  reach the store
- Restricts the source-database connection to read-only access: the
  profiler issues `SELECT` queries only. No `INSERT`, `UPDATE`,
  `DELETE`, or `DROP` codepaths exist against the source database.
  (Schema Brain's own local SQLite store is written to, of course —
  that's the cache.)
- Runs `pip-audit`, `bandit`, and `semgrep` on every PR via CI

Hardening on the roadmap (not yet shipped): host allowlisting for
SSRF, exception sanitization, federated authentication for any future
HTTP transport, and SBOM publishing.
