# Schema Brain

> The SQL-boundary safety layer for AI agents that touch real databases. Schema intelligence and LLM-enriched semantics today; validate-before-execute, PII-tagged refusal, and sub-query rewrite landing in v2.

[![CI](https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml/badge.svg)](https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Status: 0.2.0a1 (alpha preview).** Postgres + SQLite supported today. Snowflake / BigQuery / MySQL on the v1 roadmap. APIs may change before v1 — pin the version (`pip install schemabrain==0.2.0a1`) if you need stability.

---

## The problem

AI agents fail when querying real production databases:

1. **Schemas don't fit in context** — a 300-table schema is 50k+ tokens of `CREATE TABLE` alone.
2. **Column names are cryptic** — `acct_dim_v3`, `pmt_fct_h`, `cust_id_v2_legacy`.
3. **Joins aren't obvious** — which FK is the "right" one when there are three?
4. **Data has shapes** — `status` could be 5 enum values, 50, or a free-text mess.

Schema Brain fixes all four and serves the result through a stable MCP tool surface that any agent (Claude Desktop, Anthropic SDK, custom) can call.

**The bigger problem behind these** — database MCPs running as the credentialed role, prompt injection escalating to SQLi (Anthropic Postgres MCP's published NPM/Docker artifacts shipped an unpatched SQL injection at archival per Datadog Security Labs; Supabase MCP enables data exfil under documented conditions), no PII-aware refusal at the SQL boundary — is what Schema Brain is being built to address at the SQL-boundary safety layer in v2. The schema intelligence shipping today is the substrate that layer needs. See [Where this is going](#where-this-is-going).

## What it does

- Indexes your database schema, profiles each column, and generates a one-paragraph LLM description per column (Claude Haiku 4.5 by default; Sonnet 4.6 for cryptic abbreviations).
- Embeds the descriptions locally with `BAAI/bge-small-en-v1.5` via `fastembed` — no second API vendor.
- Stores everything in a single SQLite file. No Qdrant, no Redis, no ops.
- Serves five MCP tools: [`find_relevant_tables`, `describe_table`, `describe_column`, `suggest_joins`, `get_example_queries`](docs/mcp-tools.md). Every response includes a token estimate so agents can budget context.
- Mines observed queries from `pg_stat_statements` so `get_example_queries` returns the SQL agents (or humans) have actually run against your tables — not invented examples.

---

## Where this is going

Schema Brain is being built as the **SQL-boundary safety layer for AI agents** — the layer that parses what your agent is about to ask the database and refuses (or rewrites) before it runs.

That layer needs a semantic substrate underneath it. You can't refuse "this query touches PII" without knowing which columns are PII. You can't rewrite "join through this junction" without canonical-join definitions. You can't validate a metric without knowing its grain.

So the engineering order is **schema intelligence → semantic substrate → safety primitives:**

- **v0 / v0.5 — schema intelligence (shipping now):** schema introspection, LLM-enriched column descriptions, embedding retrieval, query-log mining via `pg_stat_statements`, and 5 MCP tools including `get_example_queries` returning observed SQL.
- **v1 — semantic substrate:** entities, metrics, canonical joins as first-class persisted definitions. LLM-suggested from observed data; user-confirmed in YAML.
- **v2 — safety wedge:** PII-tagged refusal, `validate_query` before execute, `execute` with row/cost/timeout caps, **sub-query refusal with recovery** (parse agent SQL, refuse just the unsafe fragment with a suggested rewrite). No shipped competitor as of mid-2026.

Today the product is schema intelligence. The safety layer is the trajectory, not a current claim. If you need safety primitives now, this isn't ready yet — track the v2 roadmap.

---

## How it compares

The open-source landscape thinned in 2026: Vanna's public repo was frozen as the project went commercial, and the reference Postgres MCP server was archived in 2025 with no first-party successor named. The live landscape today:

| Project | License | First-party MCP | Status |
|---|---|---|---|
| **Schema Brain** | MIT | ✅ | Active — `0.2.0a1` alpha |
| [Vanna AI](https://github.com/vanna-ai/vanna) | MIT (repo frozen) | ❌ | OSS archived 2026-03; project moved commercial (Vanna 2.0 / Cloud / Enterprise) |
| [Reference Postgres MCP](https://github.com/modelcontextprotocol/servers-archived) | MIT | ✅ | Archived 2025-05; no first-party successor named |
| [Atlan](https://atlan.com) | Closed-source | ✅ | SaaS-only, enterprise pricing |
| [dbt-mcp](https://github.com/dbt-labs/dbt-mcp) | Apache-2.0 | ✅ | Active — requires a dbt project |
| [WrenAI](https://github.com/canner/WrenAI) | Apache-2.0 | ❌ (roadmap) | Active — uses MDL modeling layer |

Schema Brain sits where none of these cover cleanly: **OSS + MIT + first-party MCP + no modeling layer required + introspects a live Postgres in one Python process + mines `pg_stat_statements` to surface observed SQL as agent context**.

The longer-term position is the SQL-boundary safety layer for agents (see [Where this is going](#where-this-is-going)). None of the projects above operate at the parse-agent-SQL-and-judge-against-policy layer; that's the v2 wedge.

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

> **Cost.** ~$0.0003/column with Claude Haiku 4.5. The bundled 6-table fixture indexes for **$0.0074 in 38s**; the Pagila DVD-rental sample (87 columns after partition deduplication) for **$0.0299 in 105s**. Re-indexing an unchanged schema costs **$0** — content-addressable fingerprinting skips the LLM call entirely.

## Quickstart

Five minutes from `pip install` to a working Claude Desktop integration. Three caveats up front — they tripped real users:

| Gotcha | Fix |
|---|---|
| `psql` is not on macOS by default | We use `docker exec -i sb-pg psql ...` instead — runs psql inside the postgres container, no host install needed |
| `pip install schemabrain` and the first `schemabrain index` are each silent for ~30–60s | Don't kill them. `pip` resolves ~75 wheels; the first index downloads the ONNX embedding model (~67 MB) and makes 24 LLM calls. Progress bars land in v0. |
| `ANTHROPIC_API_KEY` propagation | Run `export ANTHROPIC_API_KEY=sk-ant-...` in the same terminal you'll run `index` from |

### 1. Install

```bash
pip install schemabrain
```

Or from source if you want to hack on it:

```bash
git clone git@github.com:Arun-kc/schemabrain.git
cd schemabrain && uv sync --extra dev
```

### 2. Boot Postgres + apply the bundled fixture (or point at your own DB)

```bash
docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=local --name sb-pg postgres:16-alpine

docker exec -i sb-pg psql -U postgres -d postgres \
  < $(schemabrain fixture-path ecommerce.sql)
```

For your own database, skip docker and use your real `postgresql+psycopg://` URL.

### 3. Index it

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL="postgresql+psycopg://postgres:local@localhost:5432/postgres"

schemabrain index --url-env DATABASE_URL --store-path ./schemabrain.db
```

`--url-env` keeps the password out of `ps`, shell history, and journald.
The older `schemabrain index "<url>"` form still works for backwards
compatibility, but emits a deprecation warning when the URL contains a
password.

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
        "--url-env",
        "DATABASE_URL",
        "--store-path",
        "/ABSOLUTE/PATH/TO/schemabrain.db"
      ],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://postgres:local@localhost:5432/postgres"
      }
    }
  }
}
```

Both paths must be absolute. Quit Claude Desktop fully (Cmd+Q) and relaunch. The 🔌 tools panel should now show "schemabrain" with 4 tools.

**Or for Cursor:** drop the same `mcpServers` block into `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project-scoped). Template at [`examples/cursor_mcp_config.example.json`](examples/cursor_mcp_config.example.json). Restart Cursor.

For the headless Anthropic-SDK path, see [`examples/anthropic_demo.py`](examples/anthropic_demo.py) and [`docs/setup.md`](docs/setup.md).

---

## Roadmap

**v0.5 — finish schema intelligence (shipped):**
- Agent-UX charter v1.0 retrofit on existing tools + CI enforcement ✓
- Dev-UX foundations: rich progress UI, guided errors, `--dry-run` ✓
- Query log mining via `pg_stat_statements` (`schemabrain mine-queries`) ✓
- 5th MCP tool: `get_example_queries` — returns real SQL from your query log matching agent intent ✓

**v1 — semantic substrate:**
- Entities, metrics, canonical joins as first-class persisted definitions
- LLM-suggested entity/metric definitions from existing column descriptions + FK graph (the wedge: Cube/dbt require multi-week hand-authoring; Schema Brain collapses bootstrap to ~30 min)
- BIRD Mini-Dev automated eval harness
- Drift CLI: `schemabrain reindex --diff`
- One additional engine: Snowflake / BigQuery / MySQL
- Typer + rich CLI migration

**v2 — SQL-boundary safety wedge:**
- PII tagging beyond pattern redaction (column-level classification, agent-visible refusal at the tool boundary)
- `validate_query` — agent-emitted SQL parsed and judged against policy before execution
- `execute` with hard caps — read-only Postgres role enforced at the database layer (not just SQL string inspection), statement timeouts, row caps, per-call cost guards
- **Sub-query refusal with recovery** — parse the SQL, identify the unsafe fragment, refuse just that fragment with a suggested rewrite or alternative-tool call
- Append-only `mcp_audit` log + response provenance on every tool call

**v3 — multi-engine + control plane (commercial, gated on hosted demand):**
- Remaining engines (BigQuery / Snowflake / Redshift breadth)
- Learning loop from telemetry and reformulation patterns
- Hosted control plane with fleet-wide adversarial-signature aggregation (per-deployment refusal patterns propagate across tenants — Cloudflare-WAF model)

---

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — pipeline, retrieval contract, cache logic, cost model, eval, what's validated
- [`docs/mcp-tools.md`](docs/mcp-tools.md) — full reference for the 4 MCP tools with example responses
- [`docs/setup.md`](docs/setup.md) — Claude Desktop wiring + Anthropic SDK demo, with troubleshooting
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, TDD expectations, conventional commits, architecture invariants
- [`examples/`](examples/) — copy-paste-ready Claude Desktop config + headless agent loop using the official `mcp` Python SDK

---

## FAQ

**Does my data leave my machine?**
Only LLM-enriched column descriptions and the redacted sample values that feed them. Three regex passes (email, US SSN, credit-card-shaped digit runs) run on every sample before it leaves the profiler module — see [`schemabrain/profiler/stats.py`](schemabrain/profiler/stats.py). The Anthropic API call sends column metadata + redacted samples + sibling-column context — no raw rows, no full result sets. Embeddings are generated locally via `fastembed` (BAAI/bge-small-en-v1.5, ONNX, ~67 MB).

**Is this a semantic layer like Cube or dbt Semantic Layer?**
Today, no — Schema Brain is schema intelligence (LLM-enriched descriptions + retrieval over your physical schema). Agents see `schema.table.column`, not `entity.metric`.

The semantic substrate (first-class entities like `customer` instead of `public.users`, metrics with grain + units, canonical joins as versioned definitions) lands in v1. But the semantic layer is the **substrate**, not the headline — it's what makes the v2 SQL-boundary safety primitives possible (refuse-by-PII-tag, validate-before-execute, sub-query refusal). If you already run dbt or Cube, Schema Brain will complement them at the safety layer rather than replace them at the semantic layer; if you don't, the v1 substrate is generated for you (LLM-suggested, user-confirmed).

**What databases work today?**
Postgres 16+ (primary target) and SQLite (for development and demos). Adding Snowflake / BigQuery / MySQL is mostly a new `DataSource` implementation plus a profiler tweak — on the v1 roadmap.

**Why MCP and not a REST API?**
The consumer is an agent, not a service. MCP standardizes tool registration, schema description, and request/response transport. Agents (Claude Desktop, the Anthropic SDK, custom ones) discover Schema Brain natively and get four tools — no API wrapper, no SDK to maintain per language.

**Why local embeddings instead of OpenAI / Voyage?**
One LLM provider (Anthropic) and one local vector model is simpler than two API vendors. Embeddings change rarely, the model is bounded (one short description per column), and ~30 ms per query embed on a laptop is fast enough. Local-first also means you can index a private schema without exposing it to a second vendor.

---

## Contributing

PRs welcome. The bar is high — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the test-first / 99%-coverage / conventional-commits / architecture-invariants checklist. CI enforces all of it.

Bugs and feature requests use the structured templates in `.github/ISSUE_TEMPLATE/`. Issues without a reproduction (bugs) or a clear underlying problem (features) get closed with a request to re-open with the right info.

## License

[MIT](LICENSE).
