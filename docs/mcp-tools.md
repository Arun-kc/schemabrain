# MCP tool reference

SchemaBrain exposes twelve Pydantic-typed MCP tools split across two layers:

- **Physical-schema tools** (5) read directly from the indexed schema. Always
  available after `schemabrain index`.
- **Semantic-layer tools** (7) read curated entities, canonical joins, and
  metrics. Available once the operator has confirmed at least one entity
  (`schemabrain entities suggest --apply` or `schemabrain init`).

Every response includes a `token_estimate` so agents can budget context.
Every envelope follows the Charter status taxonomy (`success`, `empty`,
`partial`, `degraded`, `error`, `refused`) with `follow_up_hints` to chain
the next call.

---

## Physical-schema tools

### `find_relevant_tables(query: str, limit: int = 10) -> list[TableHit]`

Embedding-cosine retrieval over indexed column descriptions. Returns the
most relevant tables for a natural-language question, ranked by score.
Each hit names the matched column and surfaces its description.

```json
{
  "qualified_name": "public.orders",
  "score": 0.79,
  "best_column": "user_id",
  "best_column_description": "Unique identifier linking each order to the customer who placed it",
  "token_estimate": 49
}
```

**Note on weak matches:** A low `score` (say, < 0.85) is a *signal*, not an
error. The tool returned what semantic search found; the agent should judge
whether the score is strong enough to trust. See
[architecture.md → How retrieval works](architecture.md#how-retrieval-works)
for why this design.

### `describe_table(qualified_name: str) -> TableDescription`

Full structural and semantic dump of one table. Includes every column
(data type, nullability, default, primary-key flag, LLM description), plus
all foreign keys with target tables already pre-joined as `schema.table`.

Pass `qualified_name` as `schema.name` — e.g. `"public.orders"`. A bare
`"orders"` raises a clear error pointing you at `find_relevant_tables` to
find the schema.

### `describe_column(qualified_name: str) -> ColumnDetail`

Drill into one column. Returns its structural metadata + LLM description +
the join graph it participates in:

- `outgoing_foreign_keys`: this column joins out to where
- `incoming_foreign_keys`: which other tables reference this column

The bidirectional FK graph is the killer feature here — primary keys'
incoming FKs describe the entire join surface in one tool call.

Pass `qualified_name` as `schema.table.column` — e.g.
`"public.orders.user_id"`.

### `suggest_joins(tables: list[str], max_hops: int = 6) -> SuggestJoinsResult`

Shortest FK-graph join paths between every pair of input tables. Each
`JoinEdge` is path-oriented (`left`/`right` columns positionally aligned
for direct SQL JOIN), so the agent doesn't have to figure out FK
direction. Multi-hop paths via intermediate tables work; pairs unreachable
within `max_hops` land in `unreachable_pairs`.

```json
{
  "paths": [
    {
      "start_qualified_name": "public.orders",
      "end_qualified_name": "public.users",
      "hops": 1,
      "edges": [
        {
          "fk_name": "orders_user_id_fkey",
          "left_qualified_name": "public.orders",
          "left_columns": ["user_id"],
          "right_qualified_name": "public.users",
          "right_columns": ["id"],
          "confidence": 1.0,
          "via": "foreign_key"
        }
      ],
      "confidence": 1.0,
      "token_estimate": 145
    }
  ],
  "unreachable_pairs": [],
  "token_estimate": 152
}
```

`confidence` is `1.0` for declared FKs at v0; query-log-inferred edges
(planned for v1) will land below 1.0.

### `get_example_queries(qualified_name: str) -> ExampleQueriesResult`

Returns SQL statements that have actually been observed running against a
table, sourced from `pg_stat_statements`. Each example carries an
observation count, a sensitivity tag, and a sorted PII category list.

Run `schemabrain mine-queries --source $DATABASE_URL --store-path ./schemabrain.db`
once (or on a schedule) to populate the example-queries cache from
`pg_stat_statements`. Until then, this tool returns `status: empty` with a
recovery hint.

```json
{
  "qualified_name": "public.orders",
  "queries": [
    {
      "sql_text": "SELECT id, user_id, total_cents FROM public.orders WHERE created_at >= $1",
      "observation_count": 1247,
      "source": "pg_stat_statements",
      "sensitivity": "public",
      "pii_categories": []
    }
  ],
  "token_estimate": 132
}
```

Pass `qualified_name` as `schema.name` — same shape as `describe_table`.
Tables with no observed queries (or before `mine-queries` has run) return
an empty result with a follow-up hint.

---

## Semantic-layer tools

These tools read curated artefacts — entities, canonical joins, metrics —
written by the operator via the wizard, `schemabrain entities suggest`,
`schemabrain joins suggest`, `schemabrain metrics suggest`, or hand-edited
YAML under `apply`. When the semantic layer is empty, the tools return
`status="empty"` envelopes that route the agent back to the physical-schema
tools so it can still answer.

### `find_relevant_entities(query: str, limit: int = 10) -> list[EntityHit]`

Embedding-cosine retrieval restricted to entities. Reuses the column-level
embedding index (no second model) but ranks per *entity* by taking the MAX
cosine across the columns of each entity's bound table. Returns
domain-named hits so the agent stays in business terms.

```json
{
  "name": "customer",
  "score": 0.84,
  "qualified_table": "public.users",
  "best_column": "email",
  "best_column_description": "Primary contact email used for order confirmations and password reset",
  "token_estimate": 58
}
```

Empty envelope routes the agent differently depending on whether the
semantic layer is bare or just unmatched:

- **No entities curated yet** → `follow_up_hints: ["find_relevant_tables"]`
  (skip `list_entities`, it would also be empty).
- **Entities exist but none match** → `follow_up_hints: ["list_entities", "find_relevant_tables"]`.

Use `find_relevant_tables` instead when no entities are curated.

### `list_entities() -> list[EntitySummary]`

Returns every confirmed entity with its bound table, identity column, and
provenance (`manual`, `dbt`, `wizard`, etc.). Lean by design — no columns,
no token-heavy detail. The agent calls `describe_entity(name)` to drill in.

```json
{
  "name": "customer",
  "description": "A registered user who can place orders.",
  "qualified_table": "public.users",
  "identity": "id",
  "origin": "manual"
}
```

Returns `status="empty"` with `follow_up_hints: ["find_relevant_tables"]`
when no entities are defined yet — actionable next step is physical
discovery.

### `describe_entity(name: str) -> EntityDetail`

The entity's bound table, identity column, description, and full column
list. One round-trip gives the agent everything it needs to compose SQL
against the entity.

```json
{
  "name": "customer",
  "description": "A registered user who can place orders.",
  "qualified_table": "public.users",
  "identity": "id",
  "origin": "manual",
  "columns": [
    {"name": "id", "data_type": "bigint", "nullable": false, "description": "...", "pii_sensitivity": "public"},
    {"name": "email", "data_type": "text", "nullable": false, "description": "...", "pii_sensitivity": "public"}
  ],
  "token_estimate": 184
}
```

`pii_sensitivity` is currently hardcoded to `"public"` on every column —
the wire shape is locked so the upcoming column-level classification can
fill it without a breaking change. Block-on-sensitivity routing today
flows through `get_metric` (which propagates `pii_categories` from the
classifier), not through this field.

Pass `name` as a bare identifier — `customer`, not `public.customer`. The
schema qualifier belongs on the bound table, not the entity name. Errors
surface as `unknown_name` with a `list_entities` recovery hint.

### `resolve_join(entity_a: str, entity_b: str, name: str | None = None) -> CanonicalJoinInfo`

Returns the canonical SQL join between two entities — a ready-to-paste
`JOIN <target> AS <alias> ON ...` clause. The lookup is
direction-insensitive; the response orients per how the operator originally
confirmed the join so `sql_skeleton` renders predictably.

```json
{
  "name": "customer_orders",
  "description": "Each order belongs to the customer who placed it.",
  "source_entity": "customer",
  "target_entity": "order",
  "on": [{"source_column": "id", "target_column": "customer_id"}],
  "sql_skeleton": "JOIN public.orders AS order ON customer.id = order.customer_id",
  "token_estimate": 96
}
```

Error envelope (`status="error"`) carries one of four `error.kind` values,
each with a recovery hint the agent can act on directly:

- `ambiguous_join` — 2+ canonical joins exist (billing vs shipping
  address, primary vs secondary user). Response lists candidate names;
  re-call with the right `name`.
- `no_canonical_join` — no canonical join between the pair exists.
  Recovery routes to `suggest_joins` for an FK-graph-discovered fallback.
- `unknown_join_name` — `name` arg was passed but doesn't match any
  canonical join in the store. Response lists candidate names; pick one.
- `join_name_mismatch` — `name` arg references a real canonical join,
  but not the one between this entity pair. Response carries the actual
  canonical name for the pair; re-call with that.

Use `suggest_joins` instead when you only have physical table names.

### `get_metric(name: str, group_by: tuple[str, ...] = (), filters: tuple[MetricFilterArg, ...] = (), time_grain: str | None = None, limit: int = 1000) -> MetricResult`

Computes a pre-declared metric against the live database. Returns the
materialised rows, the parameterised SQL the compiler emitted, and a
`fingerprint` linking back to the immutable audit row. The agent never
writes SQL itself for declared metrics — SchemaBrain compiles and
parameter-binds.

```json
{
  "rows": [
    {"period": "2026-01", "category_name": "Electronics", "total_revenue": 1832145.50}
  ],
  "row_count": 12,
  "sql_skeleton": "SELECT date_trunc('month', o.created_at) AS period, c.name AS category_name, SUM(oi.quantity * oi.unit_price_cents) / 100.0 AS total_revenue FROM ... WHERE ... GROUP BY 1, 2 LIMIT :p_limit",
  "sql_params": {"p_limit": 1000},
  "fingerprint": "f3a1c89e...",
  "required_joins": ["customer_orders", "order_items_to_orders"],
  "fan_out_join_names": [],
  "pii_categories": [],
  "token_estimate": 312
}
```

- `group_by` and `filters` use `entity.column` form (e.g. `customer.id`,
  `order.created_at`) — never physical `schema.table.column`.
- `filters` is `(column, op, value)` with closed-set ops: `eq`, `ne`, `lt`,
  `lte`, `gt`, `gte`, `in`, `not_in`, `is_null`, `not_null`.
- Values bind as parameters — never inlined into SQL.
- `fan_out_join_names` flags joins where the cardinality could inflate
  rows. When the list is non-empty the envelope downgrades to
  `status="degraded"` with `confidence="MEDIUM"` — this is the
  machine-readable signal the agent should surface to the user, not the
  list itself.
- `pii_categories` propagates the MAX-sensitivity + UNION-categories of
  every column touched, and is what `--pii-block` filters against.

Use `list_entities` instead when you don't yet know what's defined.
`get_metric` only computes pre-declared metrics — it will never run
arbitrary SQL.
