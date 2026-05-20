# Schema Brain

[![CI](https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml/badge.svg)](https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml)
[![Python 3.11 | 3.12](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **The agent never writes SQL. Schema Brain does, from definitions you control.**

A pluggable semantic + SQL firewall for AI agents on Postgres. Your agent only ever sees ten read-only MCP tools — entity lookup, validated metrics, canonical-join resolution, PII-aware refusal — and Schema Brain compiles and runs the parameterized SQL on its side. Every call lands in a tamper-evident audit log.

- **One command from `pip install` to wired agent** — bare `schemabrain init` prompts for your database URL (or spins up a Docker demo Postgres if you don't have one handy) and walks the 7-stage activation wizard end-to-end. Auto-detects a dbt project and routes through the importer when one is present.
- **Validated metrics, not invented SQL** — entities, metrics, and canonical joins compile to parameterized SQL the agent never sees.
- **Pluggable into any agent loop** — Claude Desktop, Claude Code, Cursor, or your own Anthropic / OpenAI / LangGraph loop over MCP stdio. 230-LOC drop-in proof at [`examples/anthropic_demo.py`](examples/anthropic_demo.py).
- **Watch what the agent does** — `schemabrain tail` streams every tool call live; every call lands in an append-only `mcp_audit` table with a sha256 chain.

```bash
pip install schemabrain
schemabrain init
# then ask your MCP host: "list the entities Schema Brain knows about"
```

**Status: 0.3.0 (alpha).** Postgres + SQLite supported today. Snowflake / BigQuery / MySQL on the roadmap. The longer-term position is the SQL-boundary safety layer for AI agents — see [How it fits](#how-it-fits).

---

## Sample session

Real Claude Desktop session against the bundled e-commerce fixture (7 tables, 30 columns, indexed for ~$0.01):

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

Meanwhile in the operator's terminal, `schemabrain tail` streamed every tool call Claude made:

```
14:32:08.221  describe_table  qualified_name='public.users'
              → columns=4 tokens=130 in 11ms
14:32:08.224  describe_table  qualified_name='public.products'
              → columns=5 tokens=160 in 9ms
14:32:08.227  describe_table  qualified_name='public.order_items'
              → columns=5 tokens=150 in 10ms
14:32:08.231  describe_table  qualified_name='public.product_categories'
              → columns=2 tokens=70 in 8ms
```

Every call is auditable, replayable, and PII-aware. See [Observe the agent](#observe-the-agent) for the full surface.

The caveats are the differentiator. None of them — M:N double-counting, recursive-CTE awareness, free-text-status flag — is hardcoded; they fall out of letting Claude reason over the indexed descriptions. Most LLM-over-database tools confidently invent a `payments` table or shoehorn the answer into `orders.total_cents`. Schema Brain doesn't.

**Cost.** ~$0.0003/column with Claude Haiku 4.5. The bundled 7-table fixture indexes for **~$0.01 in ~40s**. The Pagila DVD-rental sample (87 columns after partition deduplication) indexes for **$0.0299 in 105s**. Re-indexing an unchanged schema is **$0** — content-addressable fingerprinting skips the LLM call entirely.

To verify Claude's SQL is mechanically correct (and that flagged caveats are the actual data behavior), see [Validating SQL Claude generates](docs/setup.md#validating-sql-claude-generates).

---

## Quickstart

Five minutes from `pip install` to a working Claude Desktop integration.

### 1. Install

```bash
pip install schemabrain
schemabrain --version
```

Or from source if you want to hack on it:

```bash
git clone git@github.com:Arun-kc/schemabrain.git
cd schemabrain && uv sync --extra dev
source .venv/bin/activate
```

### 2. Start Postgres and load the bundled fixture

```bash
docker run --rm -d \
  -p 5432:5432 \
  -e POSTGRES_PASSWORD=local \
  --name sb-pg \
  postgres:16-alpine

# Wait until Postgres accepts connections, then load the fixture.
until docker exec sb-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done

docker exec -i sb-pg psql -U postgres -d postgres \
  < "$(schemabrain fixture-path ecommerce.sql)"
```

You should see 7 `CREATE TABLE` lines scroll past. For your own database, skip this step and use your real connection URL.

> **A note on URL formats.** Schema Brain accepts standard Postgres connection URLs — bare `postgresql://`, `postgres://`, or the explicit `postgresql+psycopg://` driver form. The bare scheme is silently normalised internally to use psycopg v3, so paste whatever pgAdmin / `docker inspect` / your secret manager hands you.

### 3. Run the activation wizard

```bash
export ANTHROPIC_API_KEY=sk-ant-...           # required for entity curation
export DATABASE_URL="postgresql+psycopg://postgres:local@localhost:5432/postgres"

schemabrain init --url-env DATABASE_URL --store-path ./schemabrain.db
```

`init` is a seven-stage wizard that takes you from "I have a Postgres database" to "Claude Desktop can answer questions about it" in one command:

```
Schema Brain init — activation wizard

  [1/7] Source check
        ✓ source reachable + read-only
  [2/7] Index schema
        ✓ 7 tables, 30 columns indexed
  [3/7] Curate entities
        ✓ 6 entities suggested + applied (cost: $0.01)
  [4/7] Curate metrics
        ✓ 10 metrics suggested + applied (cost: $0.03)
  [5/7] Curate joins
        ✓ 5 canonical joins created (FK-mined, no LLM)
  [6/7] Wire host
        ✓ wrote schemabrain entry to ~/Library/Application Support/Claude/claude_desktop_config.json
  [7/7] Next
        ✓ restart your MCP host, then ask: "list the entities Schema Brain knows about"
```

<details>
<summary>If <code>ANTHROPIC_API_KEY</code> isn't set</summary>

Stage 3 skips gracefully — the wizard still wires the MCP host and the rest works. You can curate entities later:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
schemabrain entities suggest --apply --url-env DATABASE_URL --store-path ./schemabrain.db
```

Or skip entity curation entirely by passing `--no-entities` to `init`.

</details>

**What each stage does:**

- **Source check** — validates the URL is reachable + verifies the session is read-only on Postgres. Auto-detects a dbt manifest from `$DBT_PROJECT_DIR/target/manifest.json` or by walking up from the cwd for a `dbt_project.yml`. When found, stages 3 and 4 route through the dbt importer instead of the LLM.
- **Index schema** — introspects every user-visible table, fingerprints columns, persists to `./schemabrain.db`. Free by default; pass `--enrich` to add LLM column descriptions (typically $0.10–$2.00 for a 50-table schema).
- **Curate entities** — proposes domain entities via Claude Sonnet 4.6 and writes them into the store. Cap spend with `--entities-max-cost-usd N`. Opt out with `--no-entities`.
- **Curate metrics** — proposes aggregations anchored on the curated entities (measure column + agg function + grain). Cap spend with `--metrics-max-cost-usd N`. Opt out with `--no-metrics`.
- **Curate joins** — mines FK constraints + `pg_stat_statements` query log to surface canonical joins. **Deterministic — no LLM call, no cost cap.** Opt out with `--no-joins`.
- **Wire host** — writes a `schemabrain` MCP entry into Claude Desktop's config. Other MCP servers are left untouched. Existing entries trigger an interactive prompt (or pass `--yes`).
- **Next** — prints the question to ask first.

**Stages 3, 4, and 5 are best-effort:** a failure records the issue and prints a guided next step, but doesn't abort the wizard. Stages 1, 2, 6, and 7 abort on failure.

**Before each LLM-driven stage** (entities + metrics), the wizard pauses with the cost cap formatted in the prompt — Enter to continue, Ctrl-C to skip just that stage. Skip the pause in scripted runs with `--skip-llm-confirm`. The full superset `--yes` skips both the LLM pause AND the host-overwrite prompt; use it in CI. The pause auto-suppresses in non-TTY environments regardless of either flag.

**dbt as the source of truth:** force a specific manifest with `--from-dbt PATH` to route stages 3 and 4 through the dbt importer. Stage 5 (joins) is unaffected — dbt has no canonical-join concept. See [Import from dbt](#import-from-dbt) for the full surface.

**Re-running is safe.** Identical inputs → no-op for each stage.

**For Claude Code:** add `--host claude-code` to shell out to `claude mcp add` instead of editing JSON directly.

**For Cursor / Continue / Windsurf / anything else:** pass `--print-only` to print the MCP snippet without writing — paste into your host's config yourself.

### 4. Confirm it's wired

```bash
schemabrain doctor --url-env DATABASE_URL --store-path ./schemabrain.db
```

Up to 11 checks across host config, local store, and source connectivity (the full set runs for Claude Desktop on macOS/Windows with a Postgres source URL — other host/OS combinations skip the inapplicable checks). Exit 0 means everything's good. Pass `--json` for CI/monitoring output.

### 5. Restart your MCP host and ask the test question

1. Quit Claude Desktop fully — **Cmd+Q**, not just close the window. The MCP config is only read on cold start.
2. Relaunch.
3. In a new conversation:

   > list the entities Schema Brain knows about

If Claude calls `list_entities` and reports `user`, `order`, etc., you're done. If it says "I don't have access to any tool called Schema Brain," see the next section.

### 6. See what got indexed

The agent is now talking to Schema Brain. Before moving on, see what it knows — same view the agent has, no LLM call, no source connection:

```bash
schemabrain inspect --store-path ./schemabrain.db
```

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

> **Your entity names will vary.** Sonnet names entities from your schema — for the bundled fixture you'll typically see `user` (bound to `public.users`), not `customer`. Operate on the names `inspect` shows you, not the names in this sample.

Drill into one entity for the full detail view — columns, PII tags, and the joins that reach it:

```bash
schemabrain inspect user --store-path ./schemabrain.db
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

This is the operator's counterpart to the agent-facing MCP tools — anything `describe_entity` returns to Claude, `inspect` shows you locally. Use it whenever you want to verify what's curated before pointing an agent at it.

### 7. Plug into your own agent loop (optional)

Schema Brain isn't tied to Claude Desktop. The MCP server speaks standard MCP stdio, so any host that speaks MCP can drive it:

- **Claude Desktop / Claude Code / Cursor** — `init` already wrote the right config for the host you selected. For Claude Code, run `init --host claude-code` instead of editing JSON; for Continue, Windsurf, Zed, or any arbitrary host, pass `--print-only` and paste the snippet into your host's MCP config yourself.
- **Your own Anthropic SDK agent** — [`examples/anthropic_demo.py`](examples/anthropic_demo.py) is a 230-LOC drop-in that wires Claude Haiku to `schemabrain serve` over MCP stdio. Run it against your indexed store to see exactly which tools the agent calls and how it answers:

  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  python examples/anthropic_demo.py \
      --url-env DATABASE_URL \
      --store-path ./schemabrain.db \
      --question "Which tables describe customer orders?"
  ```

- **LangGraph / LlamaIndex / AutoGen / OpenAI Agents SDK** — adapt the demo's loop; the underlying MCP stdio server is the same.

An end-to-end walkthrough that exercises entities, metrics, AND canonical joins (with the bundled fixture) is at [`examples/ecommerce/`](examples/ecommerce/).

### Inspect the MCP surface (optional)

To see exactly what shape the tools expose to an agent — every argument, every JSON schema, every response envelope — without booting any agent at all, use the [official MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector \
    schemabrain serve --url-env DATABASE_URL --store-path ./schemabrain.db
```

A browser tab opens with every registered tool, its description, the input JSON schema, and a live call-and-response panel. Requires Node.js 18+. Full walkthrough in [docs/setup.md](docs/setup.md#inspecting-tool-shapes-with-the-official-mcp-inspector).

---

## If something went wrong

**`pip install schemabrain` gave me an older version.** Check `schemabrain --version`. If it's not 0.3.0, your pip cache may be stale. Run `pip install --upgrade schemabrain` or — to install from source while you wait for the latest release on PyPI — `git clone` the repo and `uv sync --extra dev`.

**`init` reports `source unreachable`.** Three common causes: (a) Postgres isn't ready yet (re-run after `pg_isready` succeeds); (b) wrong driver prefix — must be `postgresql+psycopg://`, not `postgresql://`; (c) wrong port — check `docker ps`.

**The first `init` or `schemabrain index` hangs for ~60 seconds.** Normal. The first index downloads the ONNX embedding model (~67 MB) and makes one LLM call per column. It happens once. Subsequent runs are fast.

**Claude Desktop doesn't show Schema Brain after restart.** Cmd+Q is required (close-window doesn't trigger a re-read of MCP config). After Cmd+Q and relaunch, run `schemabrain doctor` to verify the config landed. If `doctor` says everything's good but Claude Desktop still doesn't see the tool, check `~/Library/Logs/Claude/mcp*.log`.

**`get_metric` / `describe_entity` returns "no entities found".** Stage 3 of `init` was skipped (no `ANTHROPIC_API_KEY`) or `--no-entities` was passed. Run `schemabrain entities suggest --apply --url-env DATABASE_URL --store-path ./schemabrain.db`. Verify with `schemabrain inspect --store-path ./schemabrain.db`.

---

## What's next

`init` got you a working agent. From here, three groups of things you can do:

1. **[Build your semantic layer](#build-your-semantic-layer)** — curate entities, metrics, and canonical joins so the agent talks in domain terms (`get_metric("revenue", by="month")`) instead of inventing SQL.
2. **[Observe the agent](#observe-the-agent)** — `tail` for live tool-call streaming, an append-only audit log, and PII-aware refusal at the SQL boundary.
3. **[Operate over time](#operate-over-time)** — `check` to detect drift before it shows up as bad agent answers, Docker for a zero-host-install setup, plus the `inspect` browser you've already met.

The rest of this README is reference material — skim the section that matches what you want to do next.

---

## Build your semantic layer

Three concepts compose: **entities** (a domain name bound to one physical table), **metrics** (aggregations anchored on an entity, with grain), and **canonical joins** (the persisted answer to "how do entity A and entity B connect?"). All three are agent-visible via the MCP tool surface and compile to parameterized SQL the agent never sees.

The agent reaches the semantic layer through five dedicated MCP tools:

| Tool | What the agent asks it |
|---|---|
| `find_relevant_entities(query)` | "Which entities match this business concept?" — semantic search over the semantic layer. |
| `list_entities()` | "What entities exist in this database?" |
| `describe_entity(name)` | "What does this entity expose? Columns, PII sensitivity, bound table." |
| `resolve_join(entity_a, entity_b)` | "Give me the canonical SQL JOIN between these two entities." |
| `get_metric(name, by=..., filter=...)` | "Compute this aggregation. Return rows + the SQL + an audit fingerprint." |

The five physical-schema tools (`find_relevant_tables`, `describe_table`, `describe_column`, `suggest_joins`, `get_example_queries`) sit below them — see [`docs/mcp-tools.md`](docs/mcp-tools.md) for the full reference.

### Entities

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

### Metrics

`metrics suggest` mirrors `entities suggest` — same three modes, same cost guards. The LLM picks the measure column, aggregation function, optional time dimension, and grain:

```bash
schemabrain metrics suggest --url-env DATABASE_URL --dry-run
schemabrain metrics suggest --url-env DATABASE_URL --out-dir ./metric-candidates
schemabrain metrics list --store-path ./schemabrain.db
```

Metrics anchor on an entity that already exists in the store. If you haven't curated entities first, `metrics suggest` refuses with a guided error pointing at `entities apply`.

### Canonical joins

Where `entities suggest` infers WHAT to query, `joins suggest` infers HOW two entities connect. Candidates are mined from FK constraints (always present) and query-log evidence (when `schemabrain mine-queries` has populated the `example_queries` table from `pg_stat_statements`).

```bash
schemabrain joins suggest --url-env DATABASE_URL --dry-run
schemabrain joins suggest --url-env DATABASE_URL --out-dir ./join-candidates
schemabrain joins apply ./join-candidates --url-env DATABASE_URL
schemabrain joins list --store-path ./schemabrain.db
```

Once applied, the agent-facing `resolve_join` MCP tool returns the canonical join with a paste-ready `JOIN ... ON ...` skeleton. Multi-canonical-per-pair (billing vs shipping address, primary vs secondary user) is supported: pass `name=<canonical_name>` to disambiguate, or get a structured ambiguity refusal listing both.

### Import from dbt

If you already curate entities in dbt, point Schema Brain at your compiled `target/manifest.json` and dbt becomes the source of truth. Two entry points:

**During `init` (auto-detected or explicit):** the wizard's stage 1 auto-detects a manifest from `$DBT_PROJECT_DIR/target/manifest.json` or by walking up from the cwd looking for `dbt_project.yml`. When found, stages 3 (entities) and 4 (metrics) route through the importer instead of the LLM — your dbt definitions become the source of truth in one command. Force a specific manifest with `--from-dbt PATH`:

```bash
schemabrain init --url-env DATABASE_URL --from-dbt /path/to/dbt/target/manifest.json
```

Stage 5 (joins) still uses FK + query-log mining since dbt has no canonical-join concept.

**Standalone import:** if you've already run `init` (or want to import without going through the wizard), point the importer directly at a manifest:

```bash
schemabrain import dbt path/to/target/manifest.json --url-env DATABASE_URL
```

Each dbt model with a single-column primary key lands as a Schema Brain entity with `origin="dbt_import"`. Re-running is idempotent; entities that previously had `origin="manual"` or `"suggested"` flip to `"dbt_import"` (dbt takes ownership). Subsequent manual edits to dbt-owned rows are refused at the store boundary.

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

---

## Observe the agent

Every tool call is observable two ways: a live JSONL stream for real-time debugging, and an append-only audit table inside the SQLite store for after-the-fact verification.

### Live tool-call tail

When `schemabrain serve` is running, every tool call appends one JSON line to `~/.schemabrain/events.jsonl`. `schemabrain tail` reads it in real time:

```bash
# Terminal 1
schemabrain serve --url-env DATABASE_URL --store-path ./schemabrain.db

# Terminal 2
schemabrain tail
```

```
14:32:07.114  find_relevant_tables  query='customer churn last quarter'
              → matches=3 in 47ms

14:32:08.221  describe_table        qualified_name='public.users'
              → columns=12 tokens=380 in 12ms

14:32:08.890  suggest_joins         tables=['public.users', 'public.orders']
              → paths=1 in 6ms
```

Flags: `--since 30s|5m|2h|1d` (default 5m), `--no-follow` for one-shot replay, `--json` for jq-friendly output. The events file is bounded by a 10 MiB rotation.

The events file is local-only and the redactor strips connection URLs, truncates large strings, masks `get_metric` filter values and email-shaped strings — but treat it as the same trust boundary as your shell history.

See [docs/observability.md](docs/observability.md) for the full event shape and OTel integration.

### Tamper-evident audit log

Alongside the JSONL tail, every MCP tool call writes one row to an append-only `mcp_audit` table inside the local store. The table is append-only by SQL trigger, by a write-only writer connection, and by a per-row sha256 chain hash. Coherent tampering against any external archive that captured a prior hash is detectable.

```bash
schemabrain audit verify                       # exit 0 = chain clean
schemabrain audit list --since 1h --status error
```

The audit row records what tool ran, when, against which source, with what envelope status, and a structural fingerprint. Disable for a run with `--no-audit`. See [ADR 0001](docs/adr/0001-audit-row-and-pii-taxonomy.md) for the 14-field shape and the privacy guarantee the fingerprint preserves.

### OpenTelemetry export

Ship spans to Langfuse, Phoenix, Honeycomb, Grafana Tempo, or any OTLP-compatible backend by installing the optional extra and setting the standard OTel endpoint variable:

```bash
pip install 'schemabrain[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT='https://your-collector.example.com/v1/traces'
schemabrain serve --url-env DATABASE_URL --store-path ./schemabrain.db
```

Spans are named `execute_tool` with `gen_ai.*` semantic-convention attributes (`gen_ai.system`, `gen_ai.tool.name`) plus Schema Brain facets (`schemabrain.session.id`, `schemabrain.status`, `schemabrain.error_kind`). Charter `error` and `refused` statuses map to OTel `ERROR` with the `error_kind` carried as the status description for clean dashboard grouping. When the extra is missing or the endpoint is unset, OTel is silently skipped — tool calls never fail because telemetry failed. See [ADR 0004](docs/adr/0004-observability-event-bus.md) for the design.

### PII classification

`schemabrain index` tags every column with the regulator-derived PII categories from [ADR 0001](docs/adr/0001-audit-row-and-pii-taxonomy.md) — twelve categories spanning GDPR, CCPA/CPRA, HIPAA, PCI DSS, and ISO 27018. Tags propagate across every column a `get_metric` call touches (MAX-sensitivity + UNION-categories) and write into the audit row.

```bash
# Refuse any get_metric that touches `contact` or `health` columns.
schemabrain serve --pii-block contact,health
```

A blocked call returns a Charter `status="refused"` envelope with `error.kind="pii_blocked"`. The SQL is never compiled, never logged, never executed. The audit row records `refusal_reason='pii_blocked'` and the triggering categories.

Skip classification at index time with `schemabrain index ... --no-pii-classify`. Audit rows still land; the `pii_categories` column stays empty.

---

## Operate over time

The operator-side commands — see what Schema Brain knows, catch drift before it shows up as bad agent answers, run the whole thing in Docker.

### Inspect what's indexed

Covered in [Quickstart §6](#6-see-what-got-indexed) — `schemabrain inspect` is the operator browser for everything in the local store. Summary form lists entities, metrics, and joins; pass an entity name as a positional argument to drill into columns, PII tags, and reachable joins.

Exit codes: `0` rendered, `1` drilled name not found, `2` operational refusal.

### Detect drift

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

### Preview the cost of catching up

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

### Run via Docker

If you don't want a host Postgres install at all, the repo ships a `docker-compose.yml` that brings up a Postgres container with the bundled fixture, indexes it, and leaves you with a populated store on a named volume:

```bash
docker compose up
```

> **Note on ports.** The compose stack binds Postgres to host port **5433** (not 5432) so it never clashes with a developer-local Postgres already running on 5432. The Quickstart §2 standalone `docker run` recipe uses 5432 because it assumes a clean host. Pick whichever fits your setup; the MCP wiring below talks to the container over the internal Docker network (`postgres:5432`), so the host-side port mapping doesn't matter for the Claude Desktop integration.

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

---

## How it fits

### The problem

AI agents fail when querying real production databases:

1. **Schemas don't fit in context** — a 300-table schema is 50k+ tokens of `CREATE TABLE` alone.
2. **Column names are cryptic** — `acct_dim_v3`, `pmt_fct_h`, `cust_id_v2_legacy`.
3. **Joins aren't obvious** — which FK is the "right" one when there are three?
4. **Data has shapes** — `status` could be 5 enum values, 50, or a free-text mess.

Schema Brain fixes all four and serves the result through a stable MCP tool surface that any agent can call.

The bigger problem behind these — database MCPs running as the credentialed role, prompt injection escalating to SQLi, no PII-aware refusal at the SQL boundary — is what Schema Brain is being built to address at the safety layer. The schema intelligence shipping today is the substrate that layer needs.

### How it compares

The OSS landscape thinned in 2026: Vanna's public repo was frozen as the project went commercial, and the reference Postgres MCP server was archived in 2025 with no first-party successor named.

| Project | License | First-party MCP | Status |
|---|---|---|---|
| **Schema Brain** | MIT | ✅ | Active — 0.3.0 |
| [Vanna AI](https://github.com/vanna-ai/vanna) | MIT (repo frozen) | ❌ | OSS archived 2026-03; project moved commercial |
| [Reference Postgres MCP](https://github.com/modelcontextprotocol/servers-archived) | MIT | ✅ | Archived 2025-05; no first-party successor named |
| [Atlan](https://atlan.com) | Closed-source | ✅ | SaaS-only, enterprise pricing |
| [dbt-mcp](https://github.com/dbt-labs/dbt-mcp) | Apache-2.0 | ✅ | Active — requires a dbt project |
| [WrenAI](https://github.com/canner/WrenAI) | Apache-2.0 | ❌ (roadmap) | Active — uses MDL modeling layer |

Schema Brain sits where none of these cover cleanly: **OSS + MIT + first-party MCP + no modeling layer required + introspects a live Postgres in one Python process + mines `pg_stat_statements` to surface observed SQL as agent context**.

### Where it's going

Schema Brain is being built as the **SQL-boundary safety layer for AI agents** — the layer that parses what your agent is about to ask the database and refuses (or rewrites) before it runs.

That layer needs a semantic substrate underneath it. You can't refuse "this query touches PII" without knowing which columns are PII. You can't rewrite "join through this junction" without canonical-join definitions. You can't validate a metric without knowing its grain.

So the engineering order is **schema intelligence → semantic substrate → safety primitives.** The safety wedge lands in the next major milestone (see Roadmap). Today the product is schema intelligence with a working semantic substrate. If you need PII-tagged refusal and parse-before-execute now, track the roadmap — this isn't ready yet.

---

## Roadmap

> The `v0.5` / `v1` / `v2` / `v3` labels below are **roadmap milestone names**, not package versions. The package follows strict semver — `1.0.0` is reserved for an API that's been battle-tested by external users without a forced break. See [ADR-0003](docs/adr/0003-versioning-policy.md).

**v0.5 — schema intelligence (shipped):**
- Agent-UX charter v1.0 retrofit on existing tools + CI enforcement ✓
- Dev-UX foundations: rich progress UI, guided errors, `--dry-run` ✓
- Query log mining via `pg_stat_statements` (`schemabrain mine-queries`) ✓
- 5 physical-schema MCP tools including `get_example_queries` ✓

**v1 — semantic substrate (in progress):**
- Entities, metrics, canonical joins as first-class persisted definitions ✓
- LLM-suggested entity / metric / join definitions from FK graph + column descriptions ✓
- 5 semantic-layer MCP tools (`find_relevant_entities`, `list_entities`, `describe_entity`, `resolve_join`, `get_metric`) ✓
- Drift detection (`schemabrain check`) ✓
- One additional engine: Snowflake / BigQuery / MySQL
- BIRD Mini-Dev automated eval harness

**v2 — SQL-boundary safety wedge:**
- PII tagging beyond pattern redaction — column-level classification with agent-visible refusal at the tool boundary
- `validate_query` — agent-emitted SQL parsed and judged against policy before execution
- `execute` with hard caps — read-only role enforced at the database layer, statement timeouts, row caps, per-call cost guards
- **Sub-query refusal with recovery** — parse the SQL, identify the unsafe fragment, refuse just that fragment with a suggested rewrite

**v3 — multi-engine + control plane (commercial, gated on hosted demand):**
- Remaining engines (BigQuery / Snowflake / Redshift breadth)
- Learning loop from telemetry and reformulation patterns
- Hosted control plane with fleet-wide adversarial-signature aggregation

---

## Documentation

- [`docs/setup.md`](docs/setup.md) — Claude Desktop wiring + Anthropic SDK demo, with troubleshooting
- [`docs/mcp-tools.md`](docs/mcp-tools.md) — full reference for all 10 MCP tools (5 physical-schema + 5 semantic-layer)
- [`docs/architecture.md`](docs/architecture.md) — pipeline, retrieval contract, cache logic, cost model, eval
- [`docs/observability.md`](docs/observability.md) — event shape, redactor rules, OTel integration
- [`docs/adr/`](docs/adr/) — architecture decision records (audit/PII taxonomy, store protocol, versioning policy, observability bus)
- [`examples/`](examples/) — copy-paste-ready MCP configs, headless agent loop, end-to-end ecommerce walkthrough

---

## FAQ

**Does my data leave my machine?**
Only LLM-enriched column descriptions and the redacted sample values that feed them. Three regex passes (email, US SSN, credit-card-shaped digit runs) run on every sample before it leaves the profiler module — see [`schemabrain/profiler/stats.py`](schemabrain/profiler/stats.py). The Anthropic API call sends column metadata + redacted samples + sibling-column context — no raw rows. Embeddings are generated locally via `fastembed` (BAAI/bge-small-en-v1.5, ONNX, ~67 MB).

**Is this a semantic layer like Cube or dbt Semantic Layer?**
Partially. Schema Brain ships entities, metrics, and canonical joins as first-class persisted definitions today — agents call them via `list_entities`, `describe_entity`, `resolve_join`, `get_metric`. But the semantic layer isn't the headline; it's the **substrate** that makes the upcoming SQL-boundary safety primitives possible. If you already run dbt or Cube, Schema Brain complements them (point at `target/manifest.json` and dbt becomes the source of truth). If you don't, the substrate is generated for you — LLM-suggested, user-confirmed.

**What databases work today?**
Postgres 16+ (primary target) and SQLite (for development and demos). Adding Snowflake / BigQuery / MySQL is mostly a new `DataSource` implementation plus a profiler tweak — on the v1 roadmap.

**Why MCP and not a REST API?**
The consumer is an agent, not a service. MCP standardizes tool registration, schema description, and request/response transport. Agents discover Schema Brain natively and get its tool surface — no API wrapper, no SDK to maintain per language.

**Why local embeddings instead of OpenAI / Voyage?**
One LLM provider (Anthropic) and one local vector model is simpler than two API vendors. Embeddings change rarely, the model is bounded (one short description per column), and ~30 ms per query embed on a laptop is fast enough. Local-first also means you can index a private schema without exposing it to a second vendor.

---

## Contributing & License

PRs welcome. The bar is high — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the test-first / 99%-coverage / conventional-commits / architecture-invariants checklist. CI enforces all of it.

Bugs and feature requests use the structured templates in `.github/ISSUE_TEMPLATE/`. Issues without a reproduction (bugs) or a clear underlying problem (features) get closed with a request to re-open with the right info.

[MIT](LICENSE).
