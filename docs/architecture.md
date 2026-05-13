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

| Schema size | Cost | Time | Source |
|---|---|---|---|
| 6 tables / 24 columns (bundled fixture) | $0.0074 | 38 sec | measured |
| 15 tables / 87 columns (Pagila, partition children deduplicated) | $0.0299 | 105 sec | measured |
| ~50 tables / ~300 columns | $0.10–0.15 | 5–8 min | extrapolated |
| ~200 tables / ~1500 columns | $0.45–0.55 | 30–40 min | extrapolated |
| ~500 tables / ~5000 columns | $1.50–2.50 | 90–130 min | extrapolated |

Both cost and time scale near-linearly with **column count**, not table
count: across the two measured anchors, the per-column cost holds at
~$0.00035 and per-column time falls between 1.2–1.6 seconds. The
extrapolated rows assume that ratio continues; production-scale anchors
will tighten or revise it.

Hard cap configurable via `--max-cost` (default $10.00). Per-agent-query
cost depends on tool calls and turns; in our testing, typical questions
cost ~$0.005 with Haiku.

### Partitioned tables are deduplicated automatically

Declarative partition children share an identical column structure with
their parent, so enriching each one separately is purely wasted work. The
Postgres connector filters them out at `list_tables()` time using
`pg_class.relispartition`. On Pagila this dropped the index from 22
tables / 129 columns to 15 tables / 87 columns — a 34% cost reduction
and a 50% time reduction vs the naive reflection.

`get_table()` stays permissive: if a caller explicitly asks for a
partition child by name, they still get it. Only bulk listing skips them.

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

As of 2026-05-11, against two anchors: the bundled e-commerce fixture
(6 tables / 24 columns) and the Pagila DVD-rental sample (15 tables /
87 columns after declarative-partition deduplication; 22 / 129 raw):

- ✅ Indexes Postgres 16 schema with FK-aware introspection (both anchors)
- ✅ Partitioned tables are deduplicated; only the parent is enriched
- ✅ Junction (M:N) tables are detected structurally; descriptions
  explicitly warn that joining through them multiplies result rows
- ✅ Generates LLM descriptions via Anthropic Claude (Haiku 4.5 default,
  Sonnet 4.6 for cryptic columns)
- ✅ Local embeddings via `fastembed` (no second API vendor)
- ✅ All 4 MCP tools tested via Claude Desktop AND headless Anthropic SDK,
  on both anchors
- ✅ Adversarial questions handled honestly ("not in indexed schema" with
  explicit qualifier) — Pagila negative-question test correctly distinguished
  internal `payment_id` from external payment-processor transaction IDs
- ✅ Multi-hop join discovery via `suggest_joins` (Pagila: rental → customer
  → address path returned correctly)
- ✅ Cache-aware re-index ($0 on unchanged schemas)
- ✅ Fresh-machine quickstart works from a stripped shell
- ✅ Continuous integration (lint + unit + integration with 99% coverage gate)

Not yet validated:

- Production-scale schemas (~200+ tables). Extrapolation from the two
  measured anchors is in the cost-model table above.
- Snowflake / BigQuery / MySQL connectors (planned for v1)
- Long-running serve sessions (no known issues, but no soak test yet)

### M:N caveats are surfaced in junction-table descriptions

Junction (M:N association) tables are detected structurally — composite
primary key with all PK columns being FK sources to ≥2 distinct target
tables — and that detection becomes part of the column-enrichment
prompt. The resulting descriptions explicitly state that joining through
the junction multiplies result rows, surfacing the double-counting risk
downstream agents need.

Example, generated on Pagila's `film_category` (composite PK on
`(film_id, category_id)` with FKs to `film` and `category`):

> **film_id:** *"Identifier for a film in this junction table that
> links films to their categories; joining through this table
> multiplies rows by category count per film"*

> **category_id:** *"Identifies the category assigned to a film in this
> M:N junction table; joining through multiplies result rows"*

Whether the calling agent surfaces a separate **Caveat:** block in its
final answer depends on the question:

- **When the user asks about caveats** (*"what should I watch out for"*),
  Claude Haiku 4.5 reliably writes an explicit M:N caveat as the first
  numbered item, names the consequence (*"counted in both category
  totals"*), and suggests `DISTINCT` or business-rule clarification as
  the fix. This is gold-standard behavior.
- **When the user just asks for the SQL** with no priming, surfacing
  varies. Across multiple Haiku runs on identical inputs, sometimes
  the agent mentions M:N inline as a parenthetical, sometimes it omits
  it. The variance is downstream of LLM sampling, not Schema Brain.

Either way, the warning is present in every relevant column description
and is retrievable via `describe_column` / `describe_table`. Agents
performing serious analysis should be prompted to surface them.

Self-referential association tables (both PK columns FK to the same
target) do not currently qualify as junctions under this heuristic;
revisit if real-world schemas ever rely on the pattern.

Independent SQL validation (per `docs/setup.md`) remains the right
backstop for production queries.

## What's coming in v2 (substrate vs. safety layer)

The headline direction is **SQL-boundary safety for AI agents**: parse what
the agent is about to ask the database, refuse or rewrite before it runs.
The schema intelligence shipping in v0–v0.5 and the semantic layer landing
in v1 are the **substrate** that layer requires — they're not the product,
they're what makes the product possible.

Concretely, v2 adds:

- **PII tagging** beyond regex redaction — column-level classification
  (`PII`, `confidential`, `public`) flowing into agent-visible refusal at
  the tool boundary.
- **`validate_query`** — agent-emitted SQL parsed and judged against policy
  (PII intersections, allowlist, injection markers) before any execution.
- **`execute` with hard caps** — read-only Postgres role enforced at the
  database layer (not just SQL inspection), statement timeouts, row caps,
  per-call cost guards.
- **Sub-query refusal with recovery** — parse the SQL, identify the unsafe
  fragment, refuse just that fragment with a suggested rewrite or
  alternative-tool call. As of mid-2026 there's no shipped competitor on
  this exact shape (Crystal DBA validates and rejects whole queries;
  Lakera/Check Point operate at the LLM I/O boundary, not the SQL parse
  boundary).
- **Append-only `mcp_audit` log** — every tool call records
  `{call_id, tool, args, entity_ids/metric_ids resolved,`
  `source_tables touched, latency_ms, timestamp}`. Powers the v3
  fleet-signature aggregation layer.

Until v2 ships, treating Schema Brain as a safety layer would be
premature. The v0 surface gives agents better schema context, not better
SQL safety.
