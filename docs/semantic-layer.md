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

The five physical-schema tools (`find_relevant_tables`, `describe_table`, `describe_column`, `suggest_joins`, `get_example_queries`) sit below them. Full reference: [`docs/mcp-tools.md`](mcp-tools.md).

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
# rationale: users has id PK, NOT NULL email, referenced by orders.user_id
# pii_hints:
#   email: pii
version: 1
name: customer
description: A registered customer
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
