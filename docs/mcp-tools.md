# MCP tool reference

All five tools return Pydantic-typed structured output. Every response
includes a `token_estimate` so agents can budget context.

## `find_relevant_tables(query: str, limit: int = 10) -> list[TableHit]`

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

## `describe_table(qualified_name: str) -> TableDescription`

Full structural and semantic dump of one table. Includes every column
(data type, nullability, default, primary-key flag, LLM description), plus
all foreign keys with target tables already pre-joined as `schema.table`.

Pass `qualified_name` as `schema.name` — e.g. `"public.orders"`. A bare
`"orders"` raises a clear error pointing you at `find_relevant_tables` to
find the schema.

## `describe_column(qualified_name: str) -> ColumnDetail`

Drill into one column. Returns its structural metadata + LLM description +
the join graph it participates in:

- `outgoing_foreign_keys`: this column joins out to where
- `incoming_foreign_keys`: which other tables reference this column

The bidirectional FK graph is the killer feature here — primary keys'
incoming FKs describe the entire join surface in one tool call.

Pass `qualified_name` as `schema.table.column` — e.g.
`"public.orders.user_id"`.

## `suggest_joins(tables: list[str], max_hops: int = 4) -> SuggestJoinsResult`

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

## `get_example_queries(qualified_name: str) -> ExampleQueriesResult`

Returns SQL statements that have actually been observed running against a
table, sourced from `pg_stat_statements`. Each example carries an
observation count, first/last seen timestamps, and a sensitivity tag
+ PII category set.

Run `schemabrain mine-queries --source $DATABASE_URL --store-path ./schemabrain.db`
once (or on a schedule) to populate the example-queries cache from
`pg_stat_statements`. Until then, this tool returns `status: empty` with a
recovery hint.

```json
{
  "qualified_name": "public.orders",
  "items": [
    {
      "sql_text": "SELECT id, user_id, total_cents FROM public.orders WHERE created_at >= $1",
      "observation_count": 1247,
      "first_seen_at": 1736064000,
      "last_seen_at": 1736150400,
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
