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

### 3. Run the activation wizard

```bash
export DATABASE_URL="postgresql+psycopg://postgres:local@localhost:5432/postgres"

schemabrain init --url-env DATABASE_URL --store-path ./schemabrain.db
```

That's it. `init` is a five-stage wizard that takes you from "I have a
Postgres database" to "Claude Desktop can answer questions about it"
in one command:

```
Schema Brain init — activation wizard

  [1/5] Source check
        ✓ source reachable + read-only
  [2/5] Index schema
        ✓ 6 tables, 24 columns indexed
  [3/5] Curate entities
        ↷ ANTHROPIC_API_KEY not set; entity suggestion skipped
        export ANTHROPIC_API_KEY=sk-ant-... and then run
        `schemabrain entities suggest --apply`
  [4/5] Wire host
        ✓ wrote schemabrain entry to ~/Library/.../claude_desktop_config.json
        wrote: ~/Library/.../claude_desktop_config.json
  [5/5] Next
        ✓ restart your MCP host, then ask: "list the entities Schema Brain knows about"
```

What each stage does:
- **Source check** — validates the URL is reachable + verifies the session is read-only on Postgres.
- **Index schema** — introspects every user-visible table, fingerprints columns, persists to `./schemabrain.db`. Free by default; pass `--enrich` to add LLM column descriptions (typically $0.10–$2.00 for a 50-table schema).
- **Curate entities** — proposes domain entities via Claude Sonnet 4.6 and writes them into the store. Skips gracefully if `ANTHROPIC_API_KEY` isn't set, or pass `--no-entities` to opt out. Cap LLM spend with `--entities-max-cost-usd N`.
- **Wire host** — writes a `schemabrain` MCP entry into Claude Desktop's config (uses `--url-env` so passwords never land in argv, pins the version, resolves paths to absolute). Backs up any existing config on first overwrite. Other MCP servers are left untouched.
- **Next** — prints the question to ask first.

**Re-running is safe.** Identical inputs → no-op for each stage. Already-indexed source → stage 2 skips with "already indexed". Already-curated entities → stage 3 skips with "already curated". Different existing host entry → wizard prompts (interactive) or refuses without `--yes`.

**For Claude Code:** add `--host claude-code` to shell out to `claude mcp add` instead of editing JSON directly (Claude Code's supported registration path).

**For Cursor / Continue / Windsurf / anything else:** `schemabrain init --print-only` prints the snippet without writing — paste into your host's MCP config. Template paths for the major hosts are printed alongside.

Quit Claude Desktop fully (Cmd+Q) and relaunch. Ask it:

> list the entities Schema Brain knows about

**Confirm it's wired:**

```bash
schemabrain doctor --url-env DATABASE_URL --store-path ./schemabrain.db
```

`doctor` runs 11 checks across host config, local store, and source
connectivity (`SELECT 1` + read-only session verification on Postgres).
Pass `--json` for machine-readable output suitable for CI / monitoring.

For the headless Anthropic-SDK path, see [`examples/anthropic_demo.py`](examples/anthropic_demo.py) and [`docs/setup.md`](docs/setup.md).

---

## Discover entities (alpha)

After indexing, you can have Schema Brain propose **entities** —
named, validator-backed bindings from a domain concept (`customer`,
`order`) to one physical table. Entities are the substrate metrics
and canonical joins compose on top of in upcoming releases.

```bash
schemabrain entities suggest --url-env DATABASE_URL --dry-run
```

Three output modes:

| Mode | What it does | When to use |
|---|---|---|
| `--dry-run` | Print candidates with envelope (confidence, rationale, PII hints) to stdout | Preview cost and quality before committing |
| `--out-dir ./suggestions` | Write one `<entity>.yaml` per candidate plus a metadata sidecar | Edit before applying — pipe individual files through `entities apply` |
| `--apply` | Write candidates directly with `origin="suggested"` | Trust the LLM and commit |

Spend is bounded by `--max-cost-usd` (default `$1.00`) or the
`SCHEMABRAIN_MAX_LLM_COST_USD` environment variable; the run aborts
cleanly before the ceiling is breached. Pair with `--top-k N` to cap
the candidate count.

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

Once entities are in the store, the MCP server exposes them via
`list_entities` and `describe_entity` — agents see them alongside the
physical-schema tools.

---

## Import from dbt (alpha)

If you already curate entities in **dbt**, point Schema Brain at your
compiled `target/manifest.json` and dbt becomes the source of truth:

```bash
schemabrain import dbt path/to/target/manifest.json \
    --url-env DATABASE_URL
```

Each dbt model with a single-column primary key (declared via the
`constraints` syntax, a `unique` + `not_null` constraint pair, or
`tests: [unique, not_null]` on a column) lands as a Schema Brain
entity with `origin="dbt_import"`. Re-running the command is
idempotent — already-imported entities update in place; entities
that previously had `origin="manual"` or `"suggested"` flip to
`"dbt_import"` (dbt takes ownership). Subsequent manual edits to
dbt-owned rows are refused at the store boundary.

Modes:

| Flag | What it does |
|---|---|
| _(default)_ | Plan + apply. Writes entities through the store. |
| `--dry-run` | Compute the plan; write nothing. Print the bucket counts. |
| `--report report.json` | Emit a CI-friendly JSON report with per-model detail. |

Models that fail to map (no resolvable identity, non-identifier
name, or live-schema drift) are skipped — the run continues and
names each skip on stderr. dbt resource types out of v1 scope
(`metrics`, `snapshots`, `seeds`, `analyses`, `operations`,
`exposures`) are counted in the run summary so deferred work is
visible.

A bundled fixture demonstrates the flow against the same ecommerce
schema the rest of the README quickstart uses:

```bash
schemabrain import dbt $(schemabrain fixture-path ecommerce_manifest.json) \
    --url-env DATABASE_URL \
    --dry-run
```

---

## Canonical joins (alpha)

Where `entities suggest` infers WHAT to query, `joins suggest` infers
HOW two entities CONNECT. The canonical-join graph is the persisted
answer to "how do entity A and entity B join?" — mined from FK
constraints (always present) and query-log evidence (when
`mine-queries` has populated `example_queries`).

```bash
# Mine canonical-join candidates from your indexed schema
schemabrain joins suggest \
    --url-env DATABASE_URL \
    --store-path ./schemabrain.db \
    --dry-run

# Write candidate YAMLs to a directory for review-before-apply
schemabrain joins suggest --url-env DATABASE_URL --out-dir ./join-candidates

# Apply hand-authored or reviewed canonical-join YAML files
schemabrain joins apply ./join-candidates --url-env DATABASE_URL --store-path ./schemabrain.db

# Verify what landed
schemabrain joins list --store-path ./schemabrain.db
```

| Mode | Behaviour |
|---|---|
| `--dry-run` | Prints ranked candidates with provenance to stdout (paste-clean YAML stanzas). No writes. |
| `--out-dir DIR` | Writes one `<candidate_name>.yaml` per candidate plus a metadata sidecar. Edit before `joins apply`. |
| `--apply` | Writes candidates straight to the store with `origin='suggested'`. |
| `--report PATH` | Emits a JSON report covering candidates + cycle analysis (legal cycles surfaced as a note, never a refusal). Works with every mode. |

Once applied, the agent-facing `resolve_join` MCP tool returns the
canonical join with a paste-ready `JOIN ... ON ...` skeleton.
Multi-canonical-per-pair (billing vs shipping address, primary vs
secondary user) is supported: pass `name=<canonical_name>` to
disambiguate, or get a structured ambiguity refusal listing both.

---

## Watch what the agent does (alpha)

When the MCP server is running, every tool call is appended as one
JSON line to a local events file. `schemabrain tail` reads it in
real time so you can see exactly what the agent is asking for,
what answers it got, and what got refused.

```bash
# In one terminal — the server, which now writes to
# ~/.schemabrain/events.jsonl by default
schemabrain serve --url-env DATABASE_URL --store-path ./schemabrain.db

# In another terminal — the live activity stream
schemabrain tail
```

Sample output:

```
14:32:07.114  find_relevant_tables  query='customer churn last quarter'
              → matches=3 in 47ms

14:32:08.221  describe_table        qualified_name='public.users'
              → columns=12 tokens=380 in 12ms

14:32:08.890  suggest_joins         tables=['public.users', 'public.orders']
              → paths=1 in 6ms
```

Flags:

- `--since DURATION` — replay events newer than `30s`/`5m`/`2h`/`1d`,
  or an ISO 8601 timestamp with timezone. Default: 5m.
- `--follow` / `--no-follow` — keep streaming (default) vs print
  history and exit.
- `--json` — emit raw JSONL, pipe-friendly for `jq`/`awk`.
- `--events-path PATH` — point at a non-default file. Honours
  `$SCHEMABRAIN_EVENTS_PATH` when the flag is absent.

`schemabrain serve --no-events` disables emission entirely (no JSONL
file is written). The events file is bounded by a 10 MiB rotation —
on overflow the active file moves to `<path>.1` and a fresh active
file starts. Tail follows the active file via inode tracking, so
rotation is transparent.

**A note on what gets logged.** The events file is local-only. Tool
arguments are written after passing through a redactor that strips
connection URLs, truncates strings larger than 2 KiB, replaces
`get_metric` filter values with `<value>`, and replaces email-shaped
strings with `<email>`. The redactor is conservative-but-incomplete
by design — treat the events file as the same trust boundary as
your shell history. Don't paste it into a public issue without
review.

See [docs/observability.md](docs/observability.md) for the full
event shape, the redactor rules, and tips for shipping events into
existing observability stacks.

---

## Tamper-evident audit log (alpha)

Alongside the live JSONL tail, every MCP tool call writes one row to
an append-only `mcp_audit` table inside the local SQLite store. The
table is append-only by SQL trigger, by a write-only writer
connection, and by a per-row sha256 chain hash — coherent tampering
against any external archive that captured a prior hash is detectable.

The audit row records what tool ran, when, against which source, with
what envelope status, and a structural fingerprint. See
[ADR 0001](docs/adr/0001-audit-row-and-pii-taxonomy.md) for the
14-field shape, the regulatory backing for the PII taxonomy, and the
privacy guarantee the fingerprint preserves.

```bash
# Verify the chain (exit 0 = clean, 1 = mismatch found).
schemabrain audit verify

# List recent rows with filters.
schemabrain audit list --since 1h --status error
```

Disable for a `serve` run with `--no-audit`. If the writer can't be
constructed (read-only volume, missing perms), `serve` falls back to
no-audit with a stderr warning — the server is more useful without
audit than not at all.

### PII classification (alpha)

`schemabrain index` tags every column with the regulator-derived PII
categories from [ADR 0001](docs/adr/0001-audit-row-and-pii-taxonomy.md)
— twelve categories spanning GDPR, CCPA/CPRA, HIPAA, PCI DSS, and ISO
27018. Tags are produced by a heuristic regex classifier (column-name
match) at index time and stored in a separate `column_pii_tags` table.
`get_metric` propagates tags across every column the query touches
(MAX-sensitivity + UNION-categories per ADR §4) and writes the
resulting set into the audit row's `pii_categories` column — so two
calls touching different category sets produce distinct
`fingerprint` digests in `mcp_audit`.

```bash
# Refuse any get_metric that touches `contact` or `health` columns.
schemabrain serve --pii-block contact,health

# Skip classification at index time (audit rows still land; the
# pii_categories column stays empty).
schemabrain index ... --no-pii-classify
```

A blocked call returns a Charter `status="refused"` envelope with
`error.kind="pii_blocked"`; the SQL is never compiled, never logged,
never executed. The audit row records `refusal_reason='pii_blocked'`
and `pii_categories` lists the categories that triggered the refusal.

---

## Detect drift (alpha)

`schemabrain check` walks every persisted entity, metric, and
canonical join and confirms each one still matches the live source
schema. Drops or renames at the source surface as a structured drift
report before they show up as bad agent answers.

```bash
schemabrain check --url-env DATABASE_URL --store-path ./schemabrain.db
```

```
Schema Brain check — postgresql+psycopg://localhost:5432/postgres
8 entities (7 healthy) · 12 metrics (11 healthy) · 5 joins (5 healthy)

  ✗ entity   customer
        identity_column_missing  public.customers.legacy_email
        → update entity 'customer'`s `identity:` field and re-run
          `schemabrain entities apply`

  ✗ metric   total_revenue
        measure_column_missing  public.orders.total_cents
        → update metric 'total_revenue'`s `measure.column` and re-run
          `schemabrain metrics apply`

2 drifts detected.
```

Exit codes: `0` when every definition lines up with the source, `1`
when at least one drift is detected, `2` for operational refusals
(missing store, unreachable source, bad flags). Drift cascading is
suppressed — when an entity's bound table is missing entirely, the
downstream metric and join drifts on that table are suppressed so the
output stays focused on the root cause.

Pipe-friendly JSON for CI / monitoring:

```bash
schemabrain check --url-env DATABASE_URL --json | jq '.exit_code'
```

---

## Run via Docker (alpha)

The repo ships a `docker-compose.yml` that brings up a Postgres
container with the bundled e-commerce fixture, indexes it, and leaves
you with a populated store on a named volume — one command, no host
Postgres install required.

```bash
docker compose up
```

When the stack reaches `Done`, point an MCP host at the indexed store
via `docker run`:

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
        "serve",
        "--url-env", "DATABASE_URL",
        "--store-path", "/data/store.db"
      ]
    }
  }
}
```

For production use point Schema Brain at your real `DATABASE_URL` via
`schemabrain init --url-env DATABASE_URL` and skip the demo stack
entirely. Multi-platform images (`linux/amd64` + `linux/arm64`) are
published on every release to `ghcr.io/arun-kc/schemabrain` — swap the
local build for the published tag in `docker-compose.yml` to skip the
build step.

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
- [`docs/mcp-tools.md`](docs/mcp-tools.md) — full reference for the 5 MCP tools with example responses
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
