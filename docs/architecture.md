# Architecture

How Schema Brain is put together, the contracts the tool layer keeps, and what
"validated" actually means today.

## The pipeline

```mermaid
flowchart LR
    DB[(Postgres /<br/>SQLite)] --> Connector
    Connector --> Profiler
    Profiler -->|stats + PII redaction| Enricher
    Enricher -->|Haiku 4.5 / Sonnet 4.6<br/>via prompt cache| Embedder
    Embedder -->|fastembed<br/>BAAI/bge-small-en-v1.5| Store[(SQLite<br/>store)]

    Store --> Tool1[find_relevant_tables]
    Store --> Tool2[describe_table]
    Store --> Tool3[describe_column]
    Store --> Tool4[suggest_joins]

    Tool1 & Tool2 & Tool3 & Tool4 -->|MCP / stdio| Agent[Agent<br/>Claude Desktop /<br/>Anthropic SDK /<br/>custom]
```

The pipeline is single-process and synchronous. No Celery, no Redis, no
Qdrant. The store is one SQLite file you can `cp` to back up, `sqlite3`
into to inspect, or `rm` to start over.

## Cache-aware re-indexing

Re-running `schemabrain index` against an unchanged schema costs **$0.00** in
LLM calls. Each column has a content-addressable fingerprint:

```
sha256(name | type | nullable | default | position |
       fk_targets | sample_values | sibling_context | prompt_version)
```

On re-index:

| Change | Behavior |
|---|---|
| Structural unchanged + semantic unchanged | Keep cached description (no LLM call) |
| Semantic changed (new sample values, new FKs) | Re-enrich |
| Structural changed (column added / removed / retyped) | Re-enrich + mark `schema_changed_at` |
| Object missing | Set `deprecated_at = now()`. Purge after 30 days. |

The cache key includes `prompt_version` so a prompt change correctly
invalidates everything.

## How retrieval works

`find_relevant_tables` runs cosine similarity between the query embedding
and every stored column embedding. Per-table score = MAX cosine across the
table's columns (sparse-relevance heuristic — one highly-aligned column is
strong evidence). Tables with no matches above zero are dropped; ties break
alphabetically by qualified name.

### Weak matches return data, not errors

If we returned an error for "no good match," the agent couldn't tell *what*
the indexed schema contains — and listing what's available is one of the
most useful things the agent does on adversarial questions ("there's no
payments table; here's what we DO have").

Score thresholds are the agent's call, not the tool's. In our testing
(Claude Haiku 4.5 + Sonnet 4.6), the agent correctly judged "0.74 max
score = the search reaching, not a real hit" and answered honestly. A
future `match_quality` enum may land in v1 if smaller models struggle to
reason about raw scores.

## Cost model

Per `index` run, with the default Haiku 4.5 + local embeddings:

| Schema size | Approximate cost | Approximate time |
|---|---|---|
| 6 tables / 24 columns (bundled fixture) | $0.01 | 30–60 sec (incl. one-time ONNX download) |
| ~50 tables / ~300 columns | $0.10–0.20 | 1–2 min |
| ~200 tables / ~1500 columns | $0.50–1.00 | 5–10 min |
| ~500 tables / ~5000 columns | $2.00–5.00 | 20–30 min |

Hard cap configurable via `--max-cost` (default $10.00). Per-agent-query
cost depends on tool calls and turns; in our testing, typical questions
cost ~$0.005 with Haiku.

The 6-table number is measured. Larger sizes are extrapolated linearly
from one data point — they should be roughly right but aren't yet
empirically confirmed.

## Eval

Bundled 10-question golden set on the e-commerce fixture, with the local
embedding retriever:

| Metric | Embedding (default) | Keyword (baseline) |
|---|---|---|
| Recall@1 | **0.65** | 0.60 |
| Recall@3 | **0.95** | 0.95 |
| Recall@10 | **1.00** | 0.95 |

Reproduce:

```bash
schemabrain eval \
  --source "postgresql+psycopg://postgres:local@localhost:5432/postgres" \
  --store-path ./schemabrain.db
```

The eval harness is generic — it scores any `Retriever` Protocol
implementation against any golden set. Bundled with one e-commerce example;
bring your own `golden_sets/<your-schema>.json` for your real schema. The
BIRD Mini-Dev automated benchmark is on the v0 roadmap for cross-comparable
text-to-SQL execution accuracy.

## What's validated

As of 2026-05-11, against the bundled e-commerce fixture (6 tables /
24 columns):

- ✅ Indexes Postgres 16 schema with FK-aware introspection
- ✅ Generates LLM descriptions via Anthropic Claude (Haiku 4.5 default,
  Sonnet 4.6 for cryptic columns)
- ✅ Local embeddings via `fastembed` (no second API vendor)
- ✅ All 4 MCP tools tested via Claude Desktop AND headless Anthropic SDK
- ✅ Adversarial questions handled honestly ("not in indexed schema" with
  explicit qualifier)
- ✅ Cache-aware re-index ($0 on unchanged schemas)
- ✅ `pip install` + quickstart works from a fresh venv
- ✅ Continuous integration (lint + unit + integration with 99% coverage gate)

Not yet validated:

- Schemas larger than 6 tables (Pagila ~22 tables coming; production-scale
  ~200 tables to follow)
- Snowflake / BigQuery / MySQL connectors (planned for v1)
- Long-running serve sessions (no known issues, but no soak test yet)
