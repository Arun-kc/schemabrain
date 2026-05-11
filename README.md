# Schema Brain

> An MCP server that gives AI agents deep semantic understanding of any production database.

[![CI](https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml/badge.svg)](https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Status: Pre-alpha.** Repo is private; preview launch in prep. Feature-complete for Postgres + SQLite.

---

## The problem

AI agents fail when querying real production databases:

1. **Schemas don't fit in context** — a 300-table schema is 50k+ tokens of `CREATE TABLE` alone.
2. **Column names are cryptic** — `acct_dim_v3`, `pmt_fct_h`, `cust_id_v2_legacy`.
3. **Joins aren't obvious** — which FK is the "right" one when there are three?
4. **Data has shapes** — `status` could be 5 enum values, 50, or a free-text mess.

Schema Brain fixes all four and serves the result through a stable MCP tool surface that any agent (Claude Desktop, Anthropic SDK, custom) can call.

## What it does

- Indexes your database schema, profiles each column, and generates a one-paragraph LLM description per column (Claude Haiku 4.5 by default; Sonnet 4.6 for cryptic abbreviations).
- Embeds the descriptions locally with `BAAI/bge-small-en-v1.5` via `fastembed` — no second API vendor.
- Stores everything in a single SQLite file. No Qdrant, no Redis, no ops.
- Serves four MCP tools: [`find_relevant_tables`, `describe_table`, `describe_column`, `suggest_joins`](docs/mcp-tools.md). Every response includes a token estimate so agents can budget context.

---

## What it looks like in practice

Real Claude Desktop session, validated 2026-05-11 against the bundled e-commerce fixture (6 tables, 24 columns, indexed for $0.0074):

> **You:** Using Schema Brain, write me a SQL query to compute each customer's total spend by product category.
>
> **Claude:** *(calls `describe_table` on users, products, orders, order_items, product_categories — in parallel)*
>
> Here's the query. Schema is fully verified via Schema Brain.
>
> ```sql
> SELECT u.id AS customer_id, u.full_name, c.name AS category_name,
>        SUM(oi.quantity * oi.unit_price_cents) / 100.0 AS total_spend
> FROM   public.users u
> JOIN   public.orders o              ON o.user_id     = u.id
> JOIN   public.order_items oi        ON oi.order_id   = o.id
> JOIN   public.products p            ON p.id          = oi.product_id
> JOIN   public.product_categories pc ON pc.product_id = p.id
> JOIN   public.categories c          ON c.id          = pc.category_id
> GROUP BY u.id, u.full_name, c.name;
> ```
>
> Things you should know:
> - **Facts (from the schema):** Prices live on `order_items.unit_price_cents`, not `orders`. `product_categories` is a many-to-many composite-PK join table.
> - **Assumptions:** No status filter applied — `status` is free TEXT with default `'pending'`. `categories.parent_id` exists (nested categories) but I'm grouping by leaf — say if you want a recursive CTE for roll-up.
> - **Caveat:** A product in N categories has its spend counted N times (per category). Per-customer cross-category sums will exceed actual spend if products are multi-category in your data.

That's senior-engineer-grade output. None of the caveats — M:N double-counting, recursive-CTE awareness, free-text-status flag — is hardcoded; it falls out of letting Claude reason over the indexed descriptions. To verify Claude's SQL is mechanically correct (and that flagged caveats are the actual data behavior), see [Validating SQL Claude generates](docs/setup.md#validating-sql-claude-generates).

### And when the answer doesn't exist

> **You:** Using Schema Brain, where do I find payment processor transaction IDs?
>
> **Claude:** Short answer: they're not in this database — at least not in anything Schema Brain has indexed.
>
> Most likely: payments live in a different system (Stripe/Adyen dashboard), or there's a payments table that wasn't indexed, or payment IDs are stored externally and only `orders.status` reflects the result.
>
> What to do next: `SELECT table_schema, table_name FROM information_schema.tables WHERE table_name ILIKE '%payment%'` to confirm.

The qualifier — *"at least not in anything Schema Brain has indexed"* — matters. Most LLM-over-database tools confidently invent a `payments` table or shoehorn the answer into `orders.total_cents`. Schema Brain doesn't.

---

## Quickstart

Five minutes from `git clone` to a working Claude Desktop integration. Three caveats up front — they tripped real users:

| Gotcha | Fix |
|---|---|
| `psql` is not on macOS by default | We use `docker exec -i sb-pg psql ...` instead — runs psql inside the postgres container, no host install needed |
| `uv sync --extra dev` and `schemabrain index` are each silent for ~30–60s on first run | Don't kill them. `uv sync` downloads ~75 wheels; first `index` downloads the ONNX embedding model (~67 MB) and makes 24 LLM calls. Progress bars land in v0. |
| `ANTHROPIC_API_KEY` propagation | Run `export ANTHROPIC_API_KEY=sk-ant-...` in the same terminal you'll run `index` from |

### 1. Install

```bash
git clone git@github.com:Arun-kc/schemabrain.git
cd schemabrain && uv sync --extra dev
```

> PyPI publish is on the launch checklist — until then, install from source.

### 2. Boot Postgres + apply the bundled fixture (or point at your own DB)

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=local --name sb-pg postgres:16-alpine

docker exec -i sb-pg psql -U postgres -d postgres \
  < $(python -c "import schemabrain.eval, pathlib; print(pathlib.Path(schemabrain.eval.__file__).parent / 'fixtures/ecommerce.sql')")
```

For your own database, skip docker and use your real `postgresql+psycopg://` URL.

### 3. Index it

```bash
export ANTHROPIC_API_KEY=sk-ant-...

schemabrain index "postgresql+psycopg://postgres:local@localhost:5432/postgres" \
  --store-path ./schemabrain.db
```

Expect ~30–60 seconds of silence on the first run, then:

```
Indexed 6 table(s): 6 changed, 0 unchanged, 0 removed.
Columns: +24/~0/-0. LLM: 24 descriptions ($0.0074). Embeddings: 24
```

### 4. Wire into Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "/ABSOLUTE/PATH/TO/.venv/bin/schemabrain",
      "args": [
        "serve",
        "--source",
        "postgresql+psycopg://postgres:local@localhost:5432/postgres",
        "--store-path",
        "/ABSOLUTE/PATH/TO/schemabrain.db"
      ]
    }
  }
}
```

Both paths must be absolute. Quit Claude Desktop fully (Cmd+Q) and relaunch. The 🔌 tools panel should now show "schemabrain" with 4 tools.

For the headless Anthropic-SDK path, see [`examples/anthropic_demo.py`](examples/anthropic_demo.py) and [`docs/setup.md`](docs/setup.md).

---

## Roadmap

**Next:**
- Query log mining via `pg_stat_statements` — the differentiator vs schema-only competitors
- 5th MCP tool: `get_example_queries` — returns real SQL from your query log matching agent intent
- BIRD Mini-Dev automated eval harness with reproducible CI
- Drift CLI: `schemabrain reindex --diff`
- Polished CLI (typer + progress bars)

**Later (v1):**
- Semantic layer: entities, metrics, canonical joins as first-class persisted definitions
- LLM-suggested entity definitions from existing column descriptions + FK graph
- Snowflake / BigQuery / MySQL connectors
- Hosted SaaS UI
- Multi-tenant access controls

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — pipeline, retrieval contract, cache logic, cost model, eval, what's validated
- [`docs/mcp-tools.md`](docs/mcp-tools.md) — full reference for the 4 MCP tools with example responses
- [`docs/setup.md`](docs/setup.md) — Claude Desktop wiring + Anthropic SDK demo, with troubleshooting
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, TDD expectations, conventional commits, architecture invariants
- [`examples/`](examples/) — copy-paste-ready Claude Desktop config + headless agent loop using the official `mcp` Python SDK

---

## Contributing

PRs welcome. The bar is high — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the test-first / 99%-coverage / conventional-commits / architecture-invariants checklist. CI enforces all of it.

Bugs and feature requests use the structured templates in `.github/ISSUE_TEMPLATE/`. Issues without a reproduction (bugs) or a clear underlying problem (features) get closed with a request to re-open with the right info.

## License

[MIT](LICENSE).
