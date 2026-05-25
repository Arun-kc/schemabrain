# The landscape SchemaBrain fits into

## The problem

AI agents fail when querying real production databases:

1. **Schemas don't fit in context** — a 300-table schema is 50k+ tokens of `CREATE TABLE` alone.
2. **Column names are cryptic** — `acct_dim_v3`, `pmt_fct_h`, `cust_id_v2_legacy`.
3. **Joins aren't obvious** — which FK is the "right" one when there are three?
4. **Data has shapes** — `status` could be 5 enum values, 50, or a free-text mess.

SchemaBrain fixes all four and serves the result through a stable MCP tool surface that any agent can call.

The bigger problem behind these — database MCPs running as the credentialed role, prompt injection escalating to SQLi, no PII-aware refusal at the SQL boundary — is what SchemaBrain is being built to address at the safety layer. The schema intelligence shipping today is the substrate that layer needs.

## How it compares

The OSS landscape thinned in 2026: Vanna's public repo was frozen as the project went commercial, and the reference Postgres MCP server was archived in 2025 with no first-party successor named.

| Project | License | First-party MCP | Status |
|---|---|---|---|
| **SchemaBrain** | MIT | ✅ | Active — 0.4.0 |
| [Vanna AI](https://github.com/vanna-ai/vanna) | MIT (repo frozen) | ❌ | OSS archived 2026-03; project moved commercial |
| [Reference Postgres MCP](https://github.com/modelcontextprotocol/servers-archived) | MIT | ✅ | Archived 2025-05; no first-party successor named |
| [Atlan](https://atlan.com) | Closed-source | ✅ | SaaS-only, enterprise pricing |
| [dbt-mcp](https://github.com/dbt-labs/dbt-mcp) | Apache-2.0 | ✅ | Active — requires a dbt project |
| [WrenAI](https://github.com/canner/WrenAI) | Apache-2.0 | ❌ (roadmap) | Active — uses MDL modeling layer |

SchemaBrain sits where none of these cover cleanly: **OSS + MIT + first-party MCP + no modeling layer required + introspects a live Postgres in one Python process + mines `pg_stat_statements` to surface observed SQL as agent context**.

## Is this a semantic layer like Cube or dbt Semantic Layer?

Partially. SchemaBrain ships entities, metrics, and canonical joins as first-class persisted definitions today — agents call them via `list_entities`, `describe_entity`, `resolve_join`, `get_metric`. But the semantic layer isn't the headline; it's the **substrate** that makes the upcoming SQL-boundary safety primitives possible.

If you already run dbt or Cube, SchemaBrain complements them (point at `target/manifest.json` and dbt becomes the source of truth). If you don't, the substrate is generated for you — LLM-suggested, user-confirmed.

See [`docs/semantic-layer.md`](semantic-layer.md) for the builder's guide.
