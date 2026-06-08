# Security Policy

SchemaBrain is an MCP server that connects to production-grade databases.
We take vulnerability reports seriously. This document explains how to
report a vulnerability, what to expect in response, and what is in scope.

## Supported Versions

SchemaBrain is in pre-1.0 (current line: `0.4.x`). Only the latest
minor and the `main` branch receive security fixes today. When 1.0 ships,
this table will list the supported minor versions.

| Version          | Supported          |
| ---------------- | ------------------ |
| `main` (HEAD)    | :white_check_mark: |
| Latest `0.4.x`   | :white_check_mark: |
| `0.3.x` and older| :x:                |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security reports.**

Use one of these private channels instead:

1. **GitHub Private Vulnerability Reporting (preferred):**
   <https://github.com/Arun-kc/schemabrain/security/advisories/new>
   This opens a private advisory thread visible only to project maintainers.
   It is the fastest path to a fix and a coordinated CVE if warranted.

2. **Email:** `arunkc91@gmail.com` with subject prefix `[schemabrain-security]`.

A useful report includes:
- SchemaBrain version (`schemabrain --version`)
- Python version and OS
- Minimal reproduction steps
- Impact assessment in your words (what an attacker could do)
- Suggested fix, if you have one

## Response Expectations

SchemaBrain is currently maintained by one person on a part-time basis,
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
the release notes (CHANGELOG.md) for the fix release. SchemaBrain does
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
  PyPI wheel. SchemaBrain's distributable artifacts are gated by
  `pip-audit` and the deps declared in `pyproject.toml`'s `dev` extra
  do not ship to end users.
- Self-hosted deployments of SchemaBrain where the operator has
  intentionally exposed it to untrusted networks. SchemaBrain is
  designed for local-only use today; hardened multi-tenant operation is
  on the roadmap, not the current product.
- Issues that require an already-compromised host or root access to
  exploit.
- DoS attacks against an MCP client through the agent's natural
  ability to issue arbitrary tool calls — agent-side rate-limiting is
  the client's responsibility today; an SLO contract for the server
  side is on the v1 roadmap.

## Security Posture Today

SchemaBrain currently:

- Validates and canonicalizes Postgres URLs at the boundary, stripping
  credentials before any logging or display
- Reads connection URLs from `--url-env VARNAME` so passwords never
  appear in argv (and emits a deprecation warning when they do)
- Uses parameterized SQL throughout; identifier f-strings only assemble
  pre-quoted identifiers from SQLAlchemy's `identifier_preparer`
- Redacts emails, SSNs, and credit-card numbers from sample values
  before they reach the local store. Additionally, before the LLM
  enrichment prompt is built, withholds both the sample values and their
  shape signatures *entirely* for any column the name-based PII
  classifier flags — closing the egress for names, addresses, phone
  numbers, and non-US national IDs that the value-level email/SSN/CC
  redaction can't catch, for every column whose NAME signals PII.
  (Free-text columns with generic names such as `notes` or `bio` are a
  residual gap: the v1 classifier is name-based by design.)
- Restricts the source-database connection to read-only access: the
  profiler issues `SELECT` queries only. No `INSERT`, `UPDATE`,
  `DELETE`, or `DROP` codepaths exist against the source database.
  (SchemaBrain's own local SQLite store is written to, of course —
  that's the cache.)
- Runs `pip-audit`, `bandit`, and `semgrep` on every PR via CI

### Trust boundaries you should know about

Two operator-side trust assumptions are load-bearing for the current
release:

- **LLM enrichment with `--enable-sonnet`.** Column identifiers and
  default expressions from the source database flow into LLM prompts
  verbatim. A DBA with write access to the source schema can name a
  column in a way that constitutes prompt injection. Do not index
  schemas you don't control with LLM enrichment enabled.
- **Query log mining with `schemabrain mine-queries`.** Statements
  observed in `pg_stat_statements` are stored verbatim and surfaced
  to the agent through `get_example_queries`. A DBA with write
  access to a tracked database can craft a statement body (for
  example, a comment block containing instructions framed as a
  system message) that constitutes prompt injection when an agent
  later reads the mined examples. Mine only from Postgres sources
  whose workload you trust.

Hardening on the roadmap (not yet shipped): host allowlisting for
SSRF, exception sanitization, federated authentication for any future
HTTP transport, and SBOM publishing.
