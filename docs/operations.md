# Operating Schema Brain over time

`init` got you a working agent. This page covers what you do after — inspecting the local store, catching drift before it shows up as bad agent answers, previewing re-index costs, and the Docker-only path.

For live observability (tail, audit log, OTel, PII refusal), see [`docs/observability.md`](observability.md).

## Inspect what's indexed

See what the agent has — same view it has, no LLM call, no source connection:

```bash
schemabrain inspect
```

> **Your output will vary.** Entity names come from Sonnet's read of your schema (you'll typically see `user` not `customer` on the bundled fixture); join names follow your actual FK constraints. Operate on what `inspect` prints, not the names in this sample.

```
◆ store · ./schemabrain.db
7 tables · 30 columns · 6 entities · 10 metrics · 5 joins

Definitions
├── Entities (6)
│   ├── address
│   ├── category
│   ├── order
│   ├── order_item
│   ├── product
│   └── user
├── Metrics (10)
│   ├── total_revenue
│   ├── order_count
│   └── … (8 more)
└── Joins (5)
    ├── orders_user_id
    ├── order_items_order_id
    └── … (3 more)

Drill into one: `schemabrain inspect <name>`
```

Drill into one entity for the full detail view — columns, PII tags, and the joins that reach it:

```bash
schemabrain inspect user
```

```
◆ public.users · entity:user · binding id

Description:  A registered user who can place orders.

Columns:
  id          bigint       not null  pk identity  public
  email       text         not null              pii (contact)
  full_name   text         not null              pii (contact)
  created_at  timestamptz  not null              public

Related entities:
  order  outgoing  one_to_many  via `orders_user_id`
      user.id = order.user_id
```

This is the operator's counterpart to the agent-facing MCP tools — anything `describe_entity` returns to Claude, `inspect` shows you locally.

Exit codes: `0` rendered, `1` drilled name not found, `2` operational refusal.

## Detect drift

`schemabrain check` walks every persisted entity, metric, and canonical join and confirms each one still matches the live source schema. Drops or renames at the source surface as a structured drift report — before they become bad agent answers.

```bash
schemabrain check --url-env DATABASE_URL --store-path ./schemabrain.db
```

```
8 entities (7 healthy) · 12 metrics (11 healthy) · 5 joins (5 healthy)

  ✗ entity   customer
        identity_column_missing  public.customers.legacy_email
        → update entity 'customer'`s `identity:` field and re-run
          `schemabrain entities apply`

2 drifts detected.
```

Exit `0` when everything lines up, `1` when at least one drift is detected, `2` for operational refusals. Drift cascading is suppressed — when an entity's bound table is missing, downstream metric and join drifts on that table are suppressed so the output stays focused on root cause.

Pipe-friendly: `schemabrain check --url-env DATABASE_URL --json | jq '.exit_code'`.

## Preview the cost of catching up

Schedule re-indexes confidently. `schemabrain index --dry-run --since <duration>` previews what a real run would cost — no DB writes, no LLM calls, no `ANTHROPIC_API_KEY` required — and adds a freshness audit showing how much of the local store is stale relative to the chosen cutoff:

```bash
schemabrain index --url-env DATABASE_URL --store-path ./schemabrain.db \
    --dry-run --since 14d
```

```
Would index 87 table(s): 4 changed, 83 unchanged, 0 removed. Columns: +12/~6/-0. Estimated LLM: 18 descriptions ($0.0054). Estimated embeddings: 18. No changes made to the store.
Stale since 14d: 42 columns across 9 tables (estimated refresh $0.0126)
```

The "changed/unchanged" line accounts only for the source diff since the last `index` run; the "Stale since" line flags columns whose owning table was last enriched before the cutoff — useful for catching tables that haven't been re-indexed even though they haven't structurally drifted. Accepts compact durations (`30s`, `5m`, `2h`, `14d`) or ISO 8601 timestamps with timezone.

## Run via Docker

If you don't want a host Postgres install at all, the repo ships a `docker-compose.yml` that brings up a Postgres container with the bundled fixture, indexes it, and leaves you with a populated store on a named volume:

```bash
docker compose up
```

> **Note on ports.** The compose stack binds Postgres to host port **5433** (not 5432) so it never clashes with a developer-local Postgres already running on 5432. The MCP wiring below talks to the container over the internal Docker network (`postgres:5432`), so the host-side port mapping doesn't matter for the Claude Desktop integration.

Point an MCP host at the indexed store via `docker run`:

```jsonc
// ~/Library/Application Support/Claude/claude_desktop_config.json
{
  "mcpServers": {
    "schemabrain": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--network", "schemabrain_default",
        "-v", "schemabrain_sb-data:/data",
        "-e", "DATABASE_URL=postgresql+psycopg://postgres:local@postgres:5432/postgres",
        "schemabrain:local",
        "serve", "--url-env", "DATABASE_URL", "--store-path", "/data/store.db"
      ]
    }
  }
}
```

The `docker compose up` recipe builds Schema Brain from the repo's `Dockerfile`, so a checkout is all you need. A pre-built multi-platform image (`linux/amd64` + `linux/arm64`) on a public registry is on the v0.3.x roadmap so you can skip the build step.

Full Docker setup (env-var hygiene, host-uid mapping, containerised serve config) is in [`docs/setup.md`](setup.md#0b-docker-alternative-install).
