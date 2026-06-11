---
title: "Semantic layer"
description: "How entities, canonical joins, and metrics compose into business-named queries that bypass FK guessing."
---

# Building a semantic layer

Three concepts compose SchemaBrain's semantic layer:

- **Entities** — a domain name (e.g. `customer`, `order`) bound to one physical table.
- **Metrics** — aggregations anchored on an entity, with grain (e.g. `total_revenue` by `month`).
- **Canonical joins** — the persisted answer to "how do entity A and entity B connect?"

All three are agent-visible through dedicated MCP tools and compile to parameterized SQL the agent never sees.

## How the agent reaches the semantic layer

| Tool | What the agent asks it |
|---|---|
| `find_relevant_entities(query)` | "Which entities match this business concept?" — semantic search over the layer. |
| `list_entities()` | "What entities exist in this database?" |
| `describe_entity(name)` | "What does this entity expose? Columns, PII sensitivity, bound table." |
| `resolve_join(entity_a, entity_b)` | "Give me the canonical SQL JOIN between these two entities." |
| `get_metric(name, by=..., filter=..., via=...)` | "Compute this aggregation. Return rows + the SQL + an audit fingerprint." |

The five physical-schema tools (`find_relevant_tables`, `describe_table`, `describe_column`, `suggest_joins`, `get_example_queries`) sit below them. Full reference: [the MCP tool reference](/reference/mcp-tools/overview).

## Author from scratch (no LLM)

Every definition is a small YAML file you can write by hand — the `suggest` commands in the sections below are just an LLM-assisted shortcut for producing the same files. To build a layer manually, follow three steps in order.

**1. Index the source first.** Entities bind to physical tables, so the store must know the schema before any definition will apply:

```bash
schemabrain index --url-env DATABASE_URL --store-path ./schemabrain.db
```

**2. Apply in dependency order.** The store enforces references between definitions — an entity must exist before a join or metric can point at it — so apply **entities → joins → metrics**:

```bash
schemabrain entities apply ./entities --url-env DATABASE_URL --store-path ./schemabrain.db
schemabrain joins    apply ./joins    --url-env DATABASE_URL --store-path ./schemabrain.db
schemabrain metrics  apply ./metrics  --url-env DATABASE_URL --store-path ./schemabrain.db
```

A minimal three-file layer (`origin: manual` marks a hand-authored definition):

```yaml
# entities/order.yaml
version: 1
name: order
description: A customer order.
binding:
  single_table: public.orders
identity: id
origin: manual
```

```yaml
# metrics/total_revenue.yaml
version: 1
name: total_revenue
description: Sum of order totals per requested grain.
entity: order
measure:
  agg: sum
  column: total_cents
time_dimension: order.placed_at
time_grains: [day, week, month]
```

```yaml
# joins/order_customer.yaml
version: 1
name: order_customer
description: Links each order to the customer who placed it.
source_entity: order
target_entity: customer
"on":
  - source: user_id
    target: id
cardinality: many_to_one
```

**3. Verify it resolves.** Column references — a metric's measure column, a join's `on` columns, an entity's `identity` — are validated when a query **compiles**, not when it applies. So `apply` accepting a file is not proof the layer works; confirm it end to end:

```bash
schemabrain entities list --store-path ./schemabrain.db   # are all three present?
schemabrain metrics  list --store-path ./schemabrain.db
```

Then ask the agent (or call `get_metric`) for one real number. A typo in a column name surfaces here as an `unknown_name` envelope listing the valid columns — not as a silent success at apply time.

<Warning>
**Use the same database URL everywhere.** The store keys everything by a `source_connection_id` derived from the connection URL, so `schemabrain index` and every later `apply` / `serve` must pass the **same** URL — same host, port, and scheme. Index under `postgresql://…` and apply under `postgresql+psycopg://…` and they key to different stores: the second command reports an empty / unindexed source even though the tables are right there. Pick one URL form and pass it consistently.
</Warning>

## Entities

```bash
schemabrain entities suggest --url-env DATABASE_URL --dry-run
```

| Mode | What it does |
|---|---|
| `--dry-run` | Print candidates to stdout with confidence + rationale + PII hints. No writes. |
| `--out-dir ./suggestions` | Write one `<entity>.yaml` per candidate. Edit before applying. |
| `--apply` | Write candidates straight into the store. |

Spend is bounded by `--max-cost-usd` (default `$1.00`) or `$SCHEMABRAIN_MAX_LLM_COST_USD`. Pair with `--top-k N` to cap candidate count.

Sample dry-run output:

```
# confidence: high
# rationale: users has id PK, NOT NULL email, referenced by sessions.user_id
# pii_hints:
#   email: pii
version: 1
name: user
description: A workspace member account
binding:
  single_table: public.users
identity: id
origin: suggested

-- 3 candidate(s) | model: claude-sonnet-4-6 | cost: $0.0271
```

Once entities are in the store, the MCP server exposes them via `list_entities` and `describe_entity`.

## Metrics

`metrics suggest` mirrors `entities suggest` — same three modes, same cost guards. The LLM picks the measure column (or composite expression), aggregation function, optional time dimension, and grain:

```bash
schemabrain metrics suggest --url-env DATABASE_URL --dry-run
schemabrain metrics suggest --url-env DATABASE_URL --out-dir ./metric-candidates
schemabrain metrics list --store-path ./schemabrain.db
```

Metrics anchor on an entity that already exists in the store. If you haven't curated entities first, `metrics suggest` refuses with a guided error pointing at `entities apply`.

### Composite-expression measures

A metric's measure can be either a single column or a composite expression over multiple columns of the same anchor table:

```yaml
# Single column
measure:
  agg: sum
  column: amount_cents

# Composite expression (whitelist: identifiers, + - * /, parens, numeric literals)
measure:
  agg: sum
  expression: unit_price_cents * quantity
```

Exactly one of `column` or `expression` is required. PII propagation walks every column the expression references — touching one PII-tagged column taints the whole metric.

## Canonical joins

Where `entities suggest` infers WHAT to query, `joins suggest` infers HOW two entities connect. Candidates are mined from FK constraints (always present) and query-log evidence (when `schemabrain mine-queries` has populated the `example_queries` table from `pg_stat_statements`).

```bash
schemabrain joins suggest --url-env DATABASE_URL --dry-run
schemabrain joins suggest --url-env DATABASE_URL --out-dir ./join-candidates
schemabrain joins apply ./join-candidates --url-env DATABASE_URL
schemabrain joins list --store-path ./schemabrain.db
```

Once applied, the agent-facing `resolve_join` MCP tool returns the canonical join with a paste-ready `JOIN ... ON ...` skeleton. Multi-canonical-per-pair (billing vs shipping address, primary vs secondary user) is supported: pass `name=<canonical_name>` to disambiguate, or get a structured ambiguity refusal listing both.

### Multi-hop join paths

`get_metric` accepts `group_by=` columns that live on a table reachable through multiple canonical joins from the metric's anchor. The compiler BFSes the canonical-join graph (default `max_hops=6`), emits each JOIN against the previous hop's alias, and returns a topologically chain-ordered `MetricPlan.joins`. When multiple paths are equally short, the agent gets a structured `ambiguous_path` refusal — disambiguate via `via=(join_name, ...)`.

## Import from dbt

If you already curate entities in dbt, point SchemaBrain at your compiled `target/manifest.json` and dbt becomes the source of truth. Two entry points:

**During `init` (auto-detected or explicit):** the wizard's stage 1 auto-detects a manifest from `$DBT_PROJECT_DIR/target/manifest.json` or by walking up from the cwd looking for `dbt_project.yml`. When found, stages 3 (entities) and 4 (metrics) route through the importer instead of the LLM. Force a specific manifest with `--from-dbt PATH`:

```bash
schemabrain init --url-env DATABASE_URL --from-dbt /path/to/dbt/target/manifest.json
```

Stage 5 (joins) still uses FK + query-log mining since dbt has no canonical-join concept.

**Standalone import:** if you've already run `init` (or want to import without going through the wizard), point the importer directly at a manifest:

```bash
schemabrain import dbt path/to/target/manifest.json --url-env DATABASE_URL
```

Each dbt model with a single-column primary key lands as a SchemaBrain entity with `origin="dbt_import"`. Re-running is idempotent; entities that previously had `origin="manual"` or `"suggested"` flip to `"dbt_import"` (dbt takes ownership). Subsequent manual edits to dbt-owned rows are refused at the store boundary.

| Flag | Behaviour |
|---|---|
| _(default)_ | Plan + apply. |
| `--dry-run` | Compute the plan; write nothing. |
| `--report report.json` | Emit a CI-friendly JSON report. |

A bundled fixture demonstrates the flow:

```bash
schemabrain import dbt $(schemabrain fixture-path ecommerce_manifest.json) \
    --url-env DATABASE_URL --dry-run
```

## Audit and repair

The local store can be checked for corruption and repaired:

```bash
schemabrain metrics audit                  # report-only
schemabrain metrics audit --fix            # delete metrics with malformed measures
```

Useful after a manual edit to YAML candidates or a schema migration in the underlying source.
