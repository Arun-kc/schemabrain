---
title: "Threat model"
description: "SchemaBrain's attack surface, the threats we model against, what we mitigate by architecture, and what we explicitly do not defend against."
---

# Threat model

SchemaBrain's attack surface, the threats we model against, what we mitigate
today, and the residual risk we accept. This is a living document; assumptions
and mitigations are updated per release.

## Trust model

SchemaBrain runs as a local single-process MCP server on the operator's own
machine. It connects to a Postgres database the operator controls, reads
schema and sample values, calls Anthropic for enrichment, and exposes read-only
tools to an agent (Claude Desktop, Cursor, or a custom harness) over stdio.

The operator is trusted. The agent is **not** fully trusted — agents are LLMs
and can be confused, manipulated, or compromised through prompt-injection.
The database is trusted to the extent the operator's existing infrastructure
trusts it, but its **contents** (column names, comments, sample values) may
contain adversarial input.

## Asset 1: MCP tool surface

The MCP server exposes 12 tools over stdio. Each tool reads from the local
SQLite store, the Postgres source, or both. The attack surface includes the
tool arguments an agent passes, the connection string the operator configured,
and the audit log that records every call.

**Assets at risk**

- Connection URL (with embedded password)
- Source-database schema introspection results
- Indexed store contents (`~/.schemabrain/store.db`)
- Audit log integrity (`mcp_audit` table)

**Threats**

| Id | Threat | Severity |
|---|---|---|
| T1.1 | Database credentials land in argv, journald, or shell history | High |
| T1.2 | SQL injection via tool arguments (qualified names) | High |
| T1.3 | Audit log tampered after the fact to hide a tool call | Medium |
| T1.4 | URL query string smuggles a session config (`?options=...`) | Medium |
| T1.5 | Connection pool reuses a connection with poisoned session state | Medium |

**Current mitigations**

- T1.1: Connection URLs are read from environment variables only.
  `schemabrain index --url-env DBURL` accepts the env-var name; the URL itself
  never appears in argv. See [schemabrain/cli.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/cli.py)
  `_resolve_url_source`. The positional-URL form prints a deprecation warning
  that does not echo the password.
- T1.2: All qualified names flow through `_validate_ident` in
  [schemabrain/mcp/_helpers.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/mcp/_helpers.py), which enforces
  the regex `^[A-Za-z_][A-Za-z0-9_$]*$` and a 63-character bound (Postgres
  `NAMEDATALEN-1`). Tool SQL is parameterised; user input never enters a query
  string by concatenation. Identifier-only SQL fragments (table/column names
  that must be inline because Postgres does not parameterise schema objects)
  pass through `_validate_ident` first.
- T1.3: The `mcp_audit` table is append-only at the SQLite level. DDL in
  [schemabrain/audit/ddl.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/audit/ddl.py) installs
  `mcp_audit_no_update` and `mcp_audit_no_delete` triggers that raise on
  any `UPDATE` or `DELETE` against the table. Tampering requires direct
  SQLite-file write access (an OS-level compromise), at which point the
  audit chain's hash field detects mutation via `schemabrain audit verify`.
- T1.4: Connection URLs are filtered through `safe_engine_url` in
  [schemabrain/connectors/\_url.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/connectors/_url.py)
  before reaching `create_engine`. Only `sslmode`, `sslrootcert`, and
  `application_name` query parameters survive; everything else (notably
  `options=-c statement_timeout=1`) is stripped.
- T1.5: Both the profiler and the metric executor use SQLAlchemy `NullPool`
  (see [schemabrain/connectors/postgres.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/connectors/postgres.py)
  and [schemabrain/cli.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/cli.py) — `_make_metric_engine`).
  Every database call opens a fresh connection and disposes of it; there is
  no pool state to escape from.

**Residual risk**

- An operator who deliberately passes a positional URL despite the deprecation
  warning will leak credentials to argv. The fix is operator discipline plus
  the deprecation warning, not a mitigation.
- Direct filesystem access to `~/.schemabrain/` allows reading the store and
  audit log. We set the events file to mode `0600` (see
  [schemabrain/observability/bus.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/observability/bus.py));
  the store relies on OS umask. Operators on shared machines should rely on
  full-disk encryption and per-user home directories.

## Asset 2: Prompt injection into the database

During `index`, schema names, column names, column comments, and sample values
flow into the Anthropic enrichment prompt. An adversary who can write into the
database can attempt to override the system prompt or coerce the LLM into
emitting unexpected content.

**Assets at risk**

- The enrichment LLM's output (column descriptions, table descriptions)
- The eventual agent's behaviour, downstream of those descriptions

**Threats**

| Id | Threat | Severity |
|---|---|---|
| T2.1 | Column comment overrides the system prompt | Medium |
| T2.2 | Sample value contains exfiltration payload that surfaces verbatim in a description | Medium |
| T2.3 | Adversarial column name encodes an instruction the LLM follows | Low |

**Current mitigations**

- T2.1, T2.3: The enrichment prompt is structured for resistance to override.
  Schema content is enclosed in clearly delimited JSON; the system prompt
  instructs the model to produce schema-description output only. Confusion
  remains possible (LLMs are not parsers) — defence in depth is what follows.
- T2.2: Sample values flow through `redact_pii` in
  [schemabrain/profiler/stats.py](https://github.com/Arun-kc/schemabrain/blob/main/schemabrain/profiler/stats.py) before
  reaching the prompt. Email-shaped, SSN-shaped, and credit-card-shaped
  strings are replaced with `<redacted>` tokens. Generic injection payloads
  ("ignore previous instructions") survive this filter — it is a PII filter,
  not an injection filter.
- All three: Descriptions are written into the store, not executed. The
  agent reads them as context; the operator can re-run `schemabrain index`
  to overwrite a description they find suspicious; `schemabrain audit list`
  shows every retrieval if anomalous behaviour is observed downstream.

**Residual risk**

- A determined adversary controlling a column comment can probably bias the
  enrichment description's tone or content. The blast radius is bounded by
  what the description can express (free-text English; not code execution).
  Operators indexing untrusted donor schemas should treat the resulting
  descriptions as data, not as gospel.

## Asset 3: Agent pathological loops

A confused agent can call MCP tools repeatedly. The operator's exposure is
LLM credit (Anthropic side) and database load (Postgres side).

**Assets at risk**

- Operator LLM budget
- Source-database CPU and connection availability
- Postgres `pg_stat_statements` accuracy (a runaway loop pollutes it)

**Threats**

| Id | Threat | Severity |
|---|---|---|
| T3.1 | Runaway tool-call loop drains the agent's API budget | Medium |
| T3.2 | `get_metric` blow-up via repeated grain-mismatched queries | Medium |
| T3.3 | `find_relevant_tables` spammed with identical embeddings | Low |

**Current mitigations**

- T3.1: The agent's budget is enforced at the agent layer (Anthropic API
  keys, per-conversation limits). SchemaBrain's `--max-cost` default of
  $1 per `index` run caps the enrichment side; once exceeded the run halts
  with `status: degraded`. Read-side tools (`describe_table`,
  `find_relevant_tables`, etc.) are read-only from the store and cost
  effectively zero per call.
- T3.2: `get_metric` enforces a Postgres-side `statement_timeout`
  (operator-tunable via `--statement-timeout-ms`; default 30000ms /
  30s; pass `0` to disable). The value is injected into
  `connect_args.options` so it can't be overridden via URL query
  params, and a runaway query aborts with a clear `OperationalError`.
  The query compiler also refuses grain mismatches at parse time,
  before any SQL leaves the process.
- T3.2b: `get_metric` enforces an application-level row cap
  (operator-tunable via `--max-rows-per-result`; default 10000; pass
  `0` to disable). This is a payload-size guard, not a query-cost
  guard — the source DB still does the full scan, but the MCP
  response is bounded before reaching the agent. Use
  `--statement-timeout-ms` for query-cost bounding. EXPLAIN-based
  pre-execution cost caps are on the v0.5+ roadmap.
- T3.3: Read-side retrieval is stateless and bounded — every call returns
  at most `limit` rows; embeddings are cached; cosine is computed against
  a fixed-size matrix.

**Residual risk**

- A pathological agent can still issue thousands of cheap read-side calls
  in a session. The audit log records each one; the events file shows the
  pattern in `schemabrain tail`. Rate limiting on the MCP tool surface
  itself is a planned enhancement.

## Asset 4: Adversarial schema names

Operators sometimes index schemas they do not control (donor demos, customer
onboarding, evaluation suites). Schema names can contain control characters,
oversized identifiers, Unicode confusables, or embedded ANSI escape sequences.

**Assets at risk**

- Operator terminal (ANSI escapes injected into rendered output)
- Agent context window (oversized identifiers echoed back into context)
- Display integrity (Unicode confusables swap for visually identical Latin)

**Threats**

| Id | Threat | Severity |
|---|---|---|
| T4.1 | 150 KB identifier exhausts the agent's context window | High |
| T4.2 | Unicode confusable schema names mislead the operator | Low |
| T4.3 | Embedded ANSI escape sequences corrupt terminal output | Low |

**Current mitigations**

- T4.1: `_validate_ident` rejects any identifier longer than 63 characters.
  Error messages bound their echoed input via `_bounded_repr` (capped at
  three identifier lengths). An adversary cannot force a 150 KB string back
  into agent context through SchemaBrain.
- T4.2, T4.3: The identifier regex `^[A-Za-z_][A-Za-z0-9_$]*$` rejects
  control characters, ANSI escapes, and Unicode confusables outside the
  ASCII alphabet. Quoted-identifier syntax (`"Order Items"`) is not
  currently supported — the trade-off is explicit and accepted.

**Residual risk**

- A schema may legitimately contain identifiers that fail the regex
  (multi-word names with spaces, names containing diacritics). Those tables
  are rejected by SchemaBrain today; the operator must rename the column,
  use a view, or wait for a future quoted-identifier mode.

## Threats explicitly out of scope

- **Denial of service against the host process.** A misbehaving agent can
  spike CPU on the host; the operator's infrastructure (cgroups, ulimits,
  container limits) is the correct layer for that defence.
- **Filesystem attacks on `~/.schemabrain/`.** OS permissions and full-disk
  encryption are the right layer. We set the events file to mode `0600`
  and rely on the standard umask for the store.
- **Side-channel attacks on the local SQLite cache.** Out of scope for a
  local single-process tool. Relevant for a future hosted variant.
- **Supply-chain attacks on PyPI dependencies.** Closed by a separate
  programme: SECURITY.md, Dependabot, `pip-audit`, Bandit, and Semgrep
  all run in CI. See [SECURITY.md](https://github.com/Arun-kc/schemabrain/blob/main/SECURITY.md).
- **Multi-tenant isolation.** SchemaBrain is single-tenant by design.
  Multi-tenancy is a hosted-variant concern.

## How this document is maintained

This file is reviewed at every release. New mitigations are added with a
specific code-path citation; deprecated mitigations are removed; the
residual-risk sections move when the truth moves. Operators who discover
a new threat or a gap should file an issue or follow the disclosure
process in [SECURITY.md](https://github.com/Arun-kc/schemabrain/blob/main/SECURITY.md).
