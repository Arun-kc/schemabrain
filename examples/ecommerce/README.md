# End-to-end ecommerce example

A complete Schema Brain setup for the bundled ecommerce fixture
(7 tables: `users`, `addresses`, `orders`, `order_items`, `products`,
`categories`, `product_categories`). Walks from `pip install` to an
MCP agent that resolves a *validated* metric end-to-end — no
hallucinated SQL.

This is a starter pack, not a template. Real users authoring entities
and metrics for their own schemas will write something different —
that's the point of letting them, instead of inventing a schema for
them.

## What's in here

```
examples/ecommerce/
├── README.md         (this file)
├── entities/         3 entities binding domain names to tables
│   ├── customer.yaml      → public.users
│   ├── order.yaml         → public.orders
│   └── product.yaml       → public.products
├── metrics/          3 measures over the order entity
│   ├── total_revenue.yaml      sum(orders.total_cents)
│   ├── order_count.yaml        count(orders.id)
│   └── customer_count.yaml     count_distinct(orders.user_id)
└── joins/            1 canonical join
    └── customer_orders.yaml    orders.user_id → users.id (many_to_one)
```

The same YAMLs ship under `schemabrain/eval/fixtures/entities/ecommerce/`
+ `schemabrain/metrics/fixtures/ecommerce/` + `schemabrain/joins/fixtures/ecommerce/`
for use by the test harness — they're duplicated here so users find
them next to the README walkthrough.

## Prerequisites

- Schema Brain installed: `pip install schemabrain` (or
  `uv sync --extra dev` from a clone).
- A Postgres reachable on `postgresql+psycopg://...` with the bundled
  ecommerce SQL loaded. The one-liner from the top-level README:
  ```bash
  docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=local --name sb-pg postgres:16-alpine
  docker exec -i sb-pg psql -U postgres -d postgres < $(schemabrain fixture-path ecommerce.sql)
  ```
- `DATABASE_URL` exported in the shell:
  ```bash
  export DATABASE_URL='postgresql+psycopg://postgres:local@localhost:5432/postgres'
  ```

## Step-by-step

### 1. Index the schema

```bash
schemabrain index --url-env DATABASE_URL --store-path ./demo.db
```

Pulls every user-visible table into a fresh SQLite store at
`./demo.db`. A few seconds against the ecommerce fixture on a recent
laptop. No LLM calls yet — `--enrich` would add per-column
descriptions but isn't needed for this walkthrough.

### 2. Apply the entities

`entities apply` takes one YAML at a time:

```bash
for yaml in examples/ecommerce/entities/*.yaml; do
    schemabrain entities apply "$yaml" \
        --url-env DATABASE_URL --store-path ./demo.db
done
```

Each entity binds a domain name (`customer`, `order`, `product`) to
exactly one physical table. The store validates the binding (table
must exist; the named identity column must exist on it) before
writing.

### 3. Apply the canonical joins

`joins apply` accepts a directory:

```bash
schemabrain joins apply examples/ecommerce/joins \
    --url-env DATABASE_URL --store-path ./demo.db
```

The `customer_orders` join records that `orders.user_id` → `users.id`
is `many_to_one`. Future joins (e.g. `order_items_order`) follow the
same shape; this walkthrough keeps it to one for clarity.

### 4. Apply the metrics

`metrics apply` also accepts a directory:

```bash
schemabrain metrics apply examples/ecommerce/metrics \
    --url-env DATABASE_URL --store-path ./demo.db
```

Three measures anchored on the `order` entity — `total_revenue`,
`order_count`, `customer_count`. Each declares its aggregation,
measure column, and supported time grains. The store refuses any
metric whose anchor entity or measure column doesn't exist.

### 5. Confirm what landed

```bash
schemabrain inspect --store-path ./demo.db
```

Should report `7 tables · 30 columns · 3 entities · 3 metrics · 1 join`.
Drill in:

```bash
schemabrain inspect order --store-path ./demo.db
```

Renders the `order` entity's columns, its `customer_orders` join (in
the entity's direction), and its three anchored metrics.

### 6. Ask Claude

Either via the headless Anthropic SDK demo:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python examples/anthropic_demo.py \
    --source "$DATABASE_URL" \
    --store-path ./demo.db \
    --question 'What was the total revenue per month last quarter?'
```

…or, after `schemabrain init` has wired Claude Desktop (see the
top-level [README quickstart](../../README.md#quickstart) if you
haven't yet), ask the same question in the desktop UI. Claude is free
to call the validated `get_metric("total_revenue", by="month")` tool
or to compose an answer from the schema-introspection tools — exactly
which path it picks depends on the agent's reasoning. When it does
take the `get_metric` route, the SQL is compiled by Schema Brain from
the YAML you applied, not invented by the agent.

### 7. Watch the tool call live

In another terminal:

```bash
schemabrain tail --follow
```

Tail reads `~/.schemabrain/events.jsonl` by default. If you started
`schemabrain serve` with `--events-path` or `--no-events`, see
[docs/observability.md](../../docs/observability.md) for how to point
`tail` at the matching file or re-enable emission.

Each MCP call streams as one JSON line — tool name, arguments, status,
duration. Pair this with the audit table for the durable record:

```bash
schemabrain audit list --since 1h
```

## What you proved

The agent answered a domain question (*revenue per month*) without
writing SQL. Schema Brain compiled the metric from your `total_revenue`
YAML + the `customer_orders` join + the `order` entity, executed it
against the source, and returned rows. Every step is visible in
`tail`, durable in `mcp_audit`, and reproducible — the same question
asked twice produces the same `fingerprint` digest in the audit row.

That's the wedge: **the agent never wrote SQL. Schema Brain did, from
definitions you controlled.**
