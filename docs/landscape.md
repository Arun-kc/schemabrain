---
title: "Landscape"
description: "Where SchemaBrain sits in the AI-database tooling ecosystem and how it differs from adjacent approaches."
---

# The landscape SchemaBrain fits into

## The problem

AI agents fail when querying real production databases:

1. **Schemas don't fit in context** — a 300-table schema is 50k+ tokens of `CREATE TABLE` alone.
2. **Column names are cryptic** — `acct_dim_v3`, `pmt_fct_h`, `cust_id_v2_legacy`.
3. **Joins aren't obvious** — which FK is the "right" one when there are three?
4. **Data has shapes** — `status` could be 5 enum values, 50, or a free-text mess.

SchemaBrain fixes all four and serves the result through a stable MCP tool surface that any agent can call.

The bigger problem behind these — database MCPs running as the credentialed role, prompt injection escalating to SQLi, no PII-aware refusal at the SQL boundary — is what SchemaBrain addresses architecturally. There is no SQL surface for the agent to attack: the 12 typed tools accept structured arguments, the compiler emits parameterized SQL against operator-validated definitions, and the four load-bearing mechanisms (read-only by architecture, 12-category PII taxonomy, hash-chained audit, structured recovery envelope) form the SQL firewall — one proof-point of six.

## How it compares

The OSS landscape thinned in 2026: Vanna's public repo was frozen as the project went commercial, and the reference Postgres MCP server was archived in 2025 with no first-party successor named.

| Project | License | First-party MCP | Status |
|---|---|---|---|
| **SchemaBrain** | Apache-2.0 | ✅ | Active — 0.5.0 |
| [Vanna AI](https://github.com/vanna-ai/vanna) | MIT (repo frozen) | ❌ | OSS archived 2026-03; project moved commercial |
| [Reference Postgres MCP](https://github.com/modelcontextprotocol/servers-archived) | MIT | ✅ | Archived 2025-05; no first-party successor named |
| [Atlan](https://atlan.com) | Closed-source | ✅ | SaaS-only, enterprise pricing |
| [dbt-mcp](https://github.com/dbt-labs/dbt-mcp) | Apache-2.0 | ✅ | Active — requires a dbt project |
| [WrenAI](https://github.com/canner/WrenAI) | Apache-2.0 | ❌ (roadmap) | Active — uses MDL modeling layer |

SchemaBrain sits where none of these cover cleanly: **OSS + Apache-2.0 + first-party MCP + no modeling layer required + introspects a live Postgres in one Python process + mines `pg_stat_statements` to surface observed SQL as agent context**.

## Is this a semantic layer like Cube or dbt Semantic Layer?

Partially. SchemaBrain ships entities, metrics, and canonical joins as first-class persisted definitions today — agents call them via `list_entities`, `describe_entity`, `resolve_join`, `get_metric`. The trust and intelligence layer is the headline; the SQL firewall is one proof-point of six that the semantic layer makes possible. Operator-validated metric definitions are what make architectural read-only possible: the agent picks a metric by name, the compiler emits the SQL.

If you already run dbt or Cube, SchemaBrain complements them (point at `target/manifest.json` and dbt becomes the source of truth). If you don't, the definitions are generated for you — LLM-suggested, user-confirmed.

See [`docs/semantic-layer.md`](semantic-layer.md) for the builder's guide.
