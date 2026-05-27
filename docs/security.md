---
title: "Security"
description: "Procurement-friendly summary of SchemaBrain's security architecture, threat model, and disclosure process."
---

# SchemaBrain — Security Posture

> Procurement-friendly summary of SchemaBrain's security architecture, threat model, and disclosure process. For the full attack-surface walk-through with code citations, see [`docs/threat-model.md`](threat-model.md).

SchemaBrain is the SQL firewall between AI agents and your production Postgres database. This document is the summary your security team will want to read before approving the package.

---

## At a glance

| Property | Posture |
|---|---|
| **Deployment model** | Local single-process MCP server. Runs on the operator's own machine. |
| **Network egress** | None to SchemaBrain infrastructure (we do not run any). LLM enrichment during `index` calls Anthropic if the operator opts in. |
| **Source code** | MIT, open source ([github.com/Arun-kc/schemabrain](https://github.com/Arun-kc/schemabrain)). Every claim on this page is verifiable from the code. |
| **Database credential exposure** | Env-var-only. Never logged, never returned in any envelope, filtered out of SQLAlchemy connection strings via [`safe_engine_url`](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/connectors/_url.py). |
| **Wire format** | MCP over stdio (no HTTPS / SSE transport today). |
| **Multi-tenant** | No. Single-tenant by design. |
| **Hosted service** | None. Local-first is an architectural commitment, not a feature gap. |

---

## The four load-bearing mechanisms

Detail in [`docs/mechanism/`](mechanism/); summarized for security review:

### 1. Architectural read-only

The MCP surface exposes [12 tools](mechanism/read-only.md), **none of which accept SQL**. There is no `execute_query`, no `run_sql`, no `validate_query`. Agents emit structured tool calls; SchemaBrain compiles parameterized SQL from operator-validated definitions. Belt-and-suspenders: `default_transaction_read_only=on` is set at the session level on every connection ([`connectors/postgres.py`](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/connectors/postgres.py) and `cli.py` at both the serve-side metric executor and the query-log miner — [source](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/cli.py)), and `NullPool` eliminates the connection-reuse pollution surface across all three database engines in the binary.

### 2. PII propagation at the metric compiler

[12 categories](mechanism/pii-taxonomy.md) grounded in GDPR / CCPA / HIPAA / PCI DSS. Each column is tagged at index time. Tags propagate through **five surfaces** at compile time — measure columns (including composite-expression operands), time-dimension columns, `group_by` columns, filter predicates, and `JOIN ON` pairs. A `get_metric` whose path touches a blocked category refuses *before* the database is queried. Three catastrophic-leak categories (`credential`, `payment_card`, `government_id`) are blocked by default on zero-config install.

### 3. Tamper-evident audit chain

Every tool call writes a row to an append-only [`mcp_audit`](mechanism/audit-chain.md) SQLite table. Each row carries `chain_hash[N] = sha256(chain_hash[N-1] || canonical(row[N]))`. SQL triggers forbid `UPDATE`/`DELETE` at the database layer ([`audit/ddl.py:64`](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/audit/ddl.py), [`:71`](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/audit/ddl.py)). `schemabrain audit verify` re-walks the chain; exit codes are 0 (intact), 1 (mismatch found), 2 (operational error).

### 4. Structured recovery envelopes

[Typed refusal contracts](mechanism/structured-recovery.md) — the agent gets back `recovery.suggested_tool` and `recovery.suggested_args` rather than English error strings. Closed `ErrorKind` Literal of 26 values means audit pipelines can switch on `error.kind` without parsing. The refusal envelope surfaces only the *blocked* category set, not the *attempted* set — closes a probe-oracle attack where iterative `group_by` calls could reconstruct the PII tagging in O(columns) calls.

---

## Threat model (summary)

The full version with code citations is at [`docs/threat-model.md`](threat-model.md). Quick walk through the four assets:

### Asset 1 — MCP tool surface

| Threat | Mitigation |
|---|---|
| DB credentials in argv / journald / shell history | Env-var-only via `--url-env VARNAME`; positional URL is deprecated with a warning |
| SQL injection via tool arguments | All identifiers validated against `^[A-Za-z_][A-Za-z0-9_$]*$` with 63-char bound; all SQL parameterized |
| Audit log tampering | SQL triggers forbid mutation; SHA256 chain detects post-hoc rewrites via any external snapshot |
| URL query string smuggling session config | [`safe_engine_url`](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/connectors/_url.py) strips everything except `sslmode` / `sslrootcert` / `application_name` |
| Connection pool session-state escape | `NullPool` on all three engines — every call opens a fresh connection |

### Asset 2 — Prompt injection into the database

| Threat | Mitigation |
|---|---|
| Column comment overrides the enrichment system prompt | Structured JSON delimiters; descriptions are data, not code; operator can re-run `index` to overwrite |
| Sample value contains exfiltration payload | PII-shaped values (email / SSN / credit card) redacted before reaching the prompt; generic injection payloads survive (this is a PII filter, not an injection filter) |
| Adversarial column name biases the LLM | Defense-in-depth via the system prompt's structured output requirement |

**Residual risk:** an adversary controlling column comments can probably bias enrichment description tone. Blast radius bounded by what free-text English can express.

### Asset 3 — Agent pathological loops

| Threat | Mitigation |
|---|---|
| Runaway tool-call loop drains LLM budget | `--max-cost` cap on `index`; read-side tools cost effectively zero per call |
| `get_metric` blow-up via repeated grain-mismatched queries | Optional Postgres-side `statement_timeout` (operator-set via `--statement-timeout-ms`; no default); grain mismatches refused at compile time before SQL leaves the process |
| Unbounded result-set rows | Optional application-level row cap via `--max-rows-per-result` (operator-set; no default) — defense against an LLM that asks for a `SELECT *` equivalent |

### Asset 4 — Adversarial schema names

| Threat | Mitigation |
|---|---|
| 150 KB identifier exhausts agent context window | Identifier validator rejects names longer than 63 chars |
| Unicode confusable schema names mislead operator | Identifier regex `^[A-Za-z_][A-Za-z0-9_$]*$` rejects non-ASCII alphanumerics |
| Embedded ANSI escape sequences corrupt terminal output | Same regex rejects control characters |

---

## What we explicitly do NOT defend against

Honesty matters for procurement reviews. These are out of scope:

- **Denial of service against the host process.** A misbehaving agent can spike CPU on the host machine. Operator infrastructure (cgroups, ulimits, container limits) is the right layer.
- **Filesystem attacks on `~/.schemabrain/`.** OS permissions + full-disk encryption are the right layer. We set the events file to `0600` and rely on standard umask for the store.
- **Side-channel attacks on the local SQLite cache.** Out of scope for a local single-process tool.
- **Supply-chain attacks on PyPI dependencies.** Closed by a separate programme: `SECURITY.md` + Dependabot + `pip-audit` + Bandit + Semgrep in CI.
- **Multi-tenant isolation.** Single-tenant by design.
- **Network-tamper detection.** No remote sinks today. Audit chain is local-file integrity, not network-transport integrity.

---

## Compliance posture

We do not certify against SOC 2 / ISO 27001 / HIPAA / PCI DSS — those are organizational programmes, not properties of a local CLI tool. We *do* ship mechanisms that operators building toward those certifications can point at as compensating controls:

| Compliance need | What SchemaBrain provides |
|---|---|
| **Audit trail integrity** | SHA256 chain with `audit verify`; append-only SQL triggers |
| **Access logging** | Every tool call recorded in `mcp_audit` with `tool_name`, `status`, `pii_categories`, `occurred_at`, `chain_hash` |
| **PII handling** | Typed taxonomy across 12 regulatory-grounded categories; column-level redaction in `describe_entity` |
| **Read-only access** | Architectural (no write tool in the binary) + recommended read-only Postgres role + session-level `default_transaction_read_only=on` |
| **Credential protection** | Env-var-only; never logged; filtered out of connection strings |
| **Vulnerability disclosure** | Process documented at [`SECURITY.md`](https://github.com/Arun-kc/schemabrain/blob/main/SECURITY.md); GitHub Private Vulnerability Reporting is the preferred channel |

---

## What SchemaBrain does NOT have (yet)

For procurement teams comparing to enterprise products:

- **RBAC / fine-grained per-user policies.** SchemaBrain is single-operator. Multi-operator policy enforcement is a future product surface.
- **Hosted control plane / centralized audit aggregation.** No SaaS today; local-first is architecturally committed.
- **External audit anchor (`audit checkpoint`).** Manual workaround: cron `schemabrain audit verify --full` + persist the chain head hash externally.
- **EXPLAIN-based cost cap.** `--statement-timeout-ms` and `--max-rows-per-result` are the v0.4 cost guards; both are operator-set with no default. EXPLAIN dry-run is on the v0.5+ roadmap.
- **HTTPS / SSE transport.** stdio only today. Required for cloud MCP clients like ChatGPT Connectors; v0.5+ roadmap.
- **Content-aware PII classification.** v0.4 classifier is name-based with structural refinements (integer-FK guard). Sample-based content matching is roadmap.
- **Anti-prompt-injection at the data-retrieval layer.** Sample values are PII-redacted but not delimiter-wrapped or pattern-stripped against generic injection payloads. Roadmap.

---

## Reporting a vulnerability

**Do not open public GitHub issues for security reports.** Use one of these private channels:

1. **GitHub Private Vulnerability Reporting (preferred):** [github.com/Arun-kc/schemabrain/security/advisories/new](https://github.com/Arun-kc/schemabrain/security/advisories/new)
2. **Email:** `arunkc91@gmail.com` with subject prefix `[schemabrain-security]`

Full process, expected timelines, and scope at [`SECURITY.md`](https://github.com/Arun-kc/schemabrain/blob/main/SECURITY.md).

---

## Further reading

- [`docs/threat-model.md`](threat-model.md) — full attack-surface walk-through with code citations and residual-risk analysis
- [`docs/mechanism/`](mechanism/) — the five load-bearing security/architecture mechanisms in detail
- [`docs/agent-ux-charter.md`](agent-ux-charter.md) — the design contract for the MCP envelope (refusal kinds, status enum, recovery contract)
- [`SECURITY.md`](https://github.com/Arun-kc/schemabrain/blob/main/SECURITY.md) — disclosure process and supported versions
- [Comparison vs Querybear](compare/querybear.md) — competitive context on the firewall posture
- [Comparison vs Anthropic reference Postgres MCP](compare/anthropic-postgres-mcp.md) — comparison against the de-facto-default MCP Postgres server
