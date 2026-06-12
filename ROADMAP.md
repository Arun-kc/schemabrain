# Roadmap

SchemaBrain is the trust and intelligence layer between AI agents and your
database. This document is the public, living view of where it is and where
it's going. It's a companion to the condensed roadmap in the
[README](README.md#roadmap) — when the two disagree, this file is canonical.

> **Milestone names are not package versions.** The `v0.5` / `v1` / `v2` /
> `v3` labels below are roadmap milestone names. The package follows strict
> semver, and `1.0.0` is reserved for an API that external users have leaned
> on without a forced break. See [ADR-0003](docs/adr/0003-versioning-policy.md).

## The engineering order

Everything is built in one direction: **schema intelligence → semantic
substrate → trust primitives.** You can't refuse "this query touches PII"
without knowing which columns are PII. You can't answer "join through this
junction" without canonical-join definitions. You can't serve a metric
without knowing its grain. The safety story is downstream of the
intelligence story, so the intelligence ships first.

---

## Now — shipping in `v0.6.x`

What you get from `pip install schemabrain` right now:

- **MCP server, 12 read-only tools** — discovery, description, entity,
  metric, and join resolution over stdio.
- **Def-driven compilation** — the agent never writes raw SQL; answers
  compile from definitions you control, with read-only execution enforced at
  the database layer plus statement timeouts and row caps.
- **Schema-intelligence engine** — index Postgres into a local SQLite store;
  cost-capped LLM semantic enrichment (with opt-in Sonnet routing for cryptic
  columns via `--enable-sonnet`); on-device embeddings (BAAI/bge-small ONNX);
  hybrid retrieval (bge query-prefix + BM25 via RRF); entity identification
  with rationale + confidence; declared-FK, query-log, and dbt-`relationships`
  join mining; a persisted canonical join graph with multi-hop BFS; and a
  metrics layer.
- **Trust & safety** — PII classification (60 rules across 12 categories) with
  per-column confidence, tag propagation, a catastrophic-leak floor (grouping
  *by* a PII column refuses as row-level disclosure), an editable policy
  (block / redact / allow plus per-column overrides), and a tamper-evident
  sha256 hash-chained audit log with browser-verifiable RFC-6962 Merkle proofs
  and `audit verify`.
- **Dashboard, 9 surfaces** — Overview, Knowledge Graph, Entities, Data
  Dictionary, PII Ledger, Audit Viewer, Refusals, Policy, and Drift. Opt-in,
  read-only, `127.0.0.1`-only.
- **CLI** — `init`, `demo`, `index`, `import dbt`, `inspect`, `diff`, `check`,
  `entities`, `joins`, `metrics`, `policy {show, apply, tag}`, `docs`,
  `dashboard`, `doctor`, `serve`, `audit`. Distributed on PyPI (Apache-2.0)
  and as a headless Docker image.

## Later — direction, not commitments

These are explored only after the launch set is solid. Listed so the
direction is legible, not as dated promises.

**Phase 2 — differentiators**

- Query cost estimation (`EXPLAIN` of the compiled SQL)
- Tenant-isolation detection — missing-filter and cross-tenant-join checks
- Impact analysis across definitions
- Usage intelligence — hotspots and dead-table detection
- A general policy-rule grammar
- Implicit-FK discovery without query logs
- Context budgeting for tool responses

**Phase 3 — exploratory**

- Persistent agent memory
- Multi-agent coordination
- Remote MCP transport plus a thin client SDK

---

## Non-goals

What SchemaBrain is deliberately *not*, so the roadmap doesn't get read as a
promise to become these things:

- **Not a text-to-SQL tool.** The default and recommended posture is
  def-driven: the agent calls semantic tools and SchemaBrain compiles
  parameterized SQL the agent never sees. Inspecting arbitrary agent-emitted
  SQL is a possible later, opt-in lane — not the direction we're pivoting to.
- **Not a write path.** No tool accepts arbitrary SQL, and there is no
  `execute()` / `query()` against your source database. Read-only is an
  architectural property, not a config flag.
- **Not a hosted, multi-tenant service today.** SchemaBrain is local-first:
  the dashboard binds `127.0.0.1` only and the store is a local SQLite file.
  Hardened multi-tenant operation is a direction, not the current product.

## How to influence the roadmap

- **Discussions** — propose an idea or argue for a reprioritization in
  [GitHub Discussions](https://github.com/Arun-kc/schemabrain/discussions).
- **Issues** — concrete bugs and well-scoped feature requests as
  [issues](https://github.com/Arun-kc/schemabrain/issues); start with
  the `good first issue` label if you'd like to contribute.
- **Security** — never in public; see [SECURITY.md](SECURITY.md).

This is a pre-1.0 project maintained in the open under Apache-2.0. The
priorities above reflect today's thinking and will move as real usage
teaches us what matters.
