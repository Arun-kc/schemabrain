<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme-hero-dark.svg">
    <img src="docs/assets/readme-hero-light.svg" alt="SchemaBrain — the SQL firewall between AI agents and your database" width="100%">
  </picture>
</p>

<!-- <h1 align="center">schemabrain</h1> -->

<p align="center">
  <a href="https://github.com/Arun-kc/schemabrain/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Arun-kc/schemabrain/ci.yml?style=flat-square&label=CI&labelColor=0A0A0A&color=3ECF8E" alt="CI"></a>
  <a href="https://pypi.org/project/schemabrain/"><img src="https://img.shields.io/pypi/v/schemabrain?style=flat-square&label=pypi&labelColor=0A0A0A&color=3ECF8E" alt="PyPI version"></a>
  <a href="https://pypi.org/project/schemabrain/"><img src="https://img.shields.io/pypi/dm/schemabrain?style=flat-square&label=downloads&labelColor=0A0A0A&color=3ECF8E" alt="PyPI downloads"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%20%7C%203.12-0A0A0A?style=flat-square&labelColor=0A0A0A" alt="Python 3.11 | 3.12"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-0A0A0A?style=flat-square&labelColor=0A0A0A" alt="License: MIT"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-compatible-3ECF8E?style=flat-square&labelColor=0A0A0A" alt="MCP compatible"></a>
</p>

<p align="center">
  <em>Works with Claude Desktop · Claude Code · Cursor · Windsurf · any MCP host</em>
</p>

<p align="center">
  <strong>Stop giving AI agents raw database connection strings.</strong><br>
  SchemaBrain is the SQL firewall between AI agents and your production database — twelve read-only tools, validated metrics, tamper-evident audit.
</p>

> **The agent never writes SQL. SchemaBrain does, from definitions you control.**

Three guarantees that close the trust gap between AI agents and your database:

- **[Read-only by architecture](#1-read-only-by-architecture-not-configuration)** — twelve MCP tools, none of which can write. No `execute()` tool, no `query()` tool, no path from agent prompt to a write at your database.
- **[PII refusal at retrieval](#2-pii-aware-refusal-at-the-get_metric-tool-boundary)** — PII tags propagate from the physical schema through joins and metrics. If a query touches a blocked category, SchemaBrain refuses *before* the database is queried.
- **[Cryptographic audit chain](#3-tamper-evident-audit-log)** — every call, refusal, and recovery lands in a SHA256-hashed append-only log. `audit verify` exits non-zero if any past row was rewritten.

---

```bash
pip install schemabrain
schemabrain init
# then: Cmd+Q Claude Desktop, relaunch, and ask: "list the entities SchemaBrain knows about"
```

**Cost:** ~$0.03 to index the bundled demo · **$0** to re-index unchanged schemas. Detail in [Sample session](#sample-session).

**Status: 0.5.0 (alpha).** Postgres + SQLite supported today. Snowflake / BigQuery / MySQL on the roadmap.

---

## Contents

- [Quickstart](#quickstart) — 3 steps from `pip install` to a working agent
- [The firewall](#the-firewall) — what SchemaBrain enforces at the SQL boundary
- [Observability dashboard](#observability-dashboard) — read-only UI for PII, refusals, audit
- [Sample session](#sample-session) — real Claude Desktop interaction against the bundled fixture
- [Where it's going](#where-its-going) — honest disclaimer about what's not built yet
- [Roadmap](#roadmap) — shipped + in progress + planned
- [Troubleshooting](#troubleshooting) — the five most common first-run failures
- [Documentation](#documentation) — deeper guides

**Read next based on what you need:**

| Goal | Where to go |
|---|---|
| Try it on the bundled fixture | [Quickstart](#quickstart) |
| Understand the firewall properties | [The firewall](#the-firewall) |
| Wire up your MCP client | [Claude Desktop](docs/setup/claude-desktop.md) · [Claude Code](docs/setup/claude-code.md) · [Cursor](docs/setup/cursor.md) · [Windsurf](docs/setup/windsurf.md) · [ChatGPT (roadmap)](docs/setup/chatgpt.md) |
| Plug into your own agent loop | [`docs/setup/manual.md`](docs/setup/manual.md#3-wire-your-own-agent-anthropic-sdk) |
| Build a semantic layer | [`docs/semantic-layer.md`](docs/semantic-layer.md) |
| Run in production (audit, drift, Docker) | [`docs/operations.md`](docs/operations.md) |
| Observe the agent (tail, audit log, OTel) | [`docs/observability.md`](docs/observability.md) |
| Compare with Querybear / Anthropic reference Postgres MCP | [vs Querybear](docs/compare/querybear.mdx) · [vs Anthropic reference](docs/compare/anthropic-postgres-mcp.mdx) |
| Compare with Vanna / Atlan / dbt-mcp / WrenAI | [`docs/landscape.md`](docs/landscape.md) |

---

## Quickstart

Three steps from `pip install` to a working Claude Desktop integration. If you paste your own Postgres URL — no Docker needed, ~30s. Press Enter for the bundled demo and `init` invokes Docker + downloads a ~67 MB embedding model first time; ~45s once cached.

### 1. Install

```bash
pip install schemabrain
schemabrain --version
```

Source install (`git clone` + [`uv`](https://docs.astral.sh/uv/) `sync --extra dev`) is documented in [`docs/setup.md`](docs/setup.md#1-install--run-the-wizard).

### 2. Run the activation wizard

```bash
schemabrain init
```

`init` is a seven-stage wizard that takes you from "I have a Postgres database" to "Claude Desktop can answer questions about it" in one command. On first run it prompts for what it needs:

- **A Postgres URL** — paste your own connection string, or press **Enter** to spin up a local demo Postgres container with the bundled SaaS fixture (Docker is invoked automatically; idempotent on re-runs).
- **An `ANTHROPIC_API_KEY`** — optional. Skip and the wizard still wires Claude Desktop. On the **demo path**, entities + metrics + joins are pre-curated from a bundled YAML pack — the semantic layer works zero-config. On **your own database**, entity curation can run later via `schemabrain entities suggest --apply` once you have a key.

```
SchemaBrain init — activation wizard

  [1/7] Source check       ✓ source reachable + read-only
  [2/7] Index schema       ✓ 12 tables, 84 columns indexed
  [3/7] Curate entities    ✓ 12 entities applied (bundled demo pack)
  [4/7] Curate metrics     ✓ 5 metrics applied (bundled demo pack)
  [5/7] Curate joins       ✓ 8 canonical joins applied (bundled demo pack)
  [6/7] Wire host          ✓ wrote schemabrain entry to claude_desktop_config.json
                           (default; switch with --host claude-code|cursor|windsurf|manual)
  [7/7] Next               ✓ restart your MCP host, then ask: "list the entities SchemaBrain knows about"
```

Full wizard reference (stages explained, flags, dbt auto-detection, `--print-only` for non-Claude-Desktop hosts, `--no-entities` / `--no-metrics` / `--no-joins` opt-outs, cost-cap pauses): [`docs/setup.md`](docs/setup.md#2-what-the-wizard-does).

### 3. Restart Claude Desktop and ask

1. Quit Claude Desktop fully — **Cmd+Q**, not just close the window. The MCP config is only read on cold start.
2. Relaunch.
3. New conversation:

   > list the entities SchemaBrain knows about

If Claude calls `list_entities` and reports `user`, `order`, etc., you're done. If not, see [Troubleshooting](#troubleshooting).

After the wizard, `schemabrain inspect` shows what the agent has and `schemabrain tail` streams every tool call live — see [`docs/operations.md`](docs/operations.md).

---

## The firewall

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme-architecture-compact-dark.svg">
    <img src="docs/assets/readme-architecture-compact-light.svg" alt="SchemaBrain architecture: agent talks to SchemaBrain over MCP stdio (12 read-only tools); SchemaBrain emits parameterized SQL to Postgres; the SchemaBrain boundary is the trust boundary; audit log is tamper-evident." width="900">
  </picture>
</p>

Six properties SchemaBrain enforces at the SQL boundary today:

<table>
<tr>
<td width="50%" valign="top">

### 1. Read-only by architecture, not configuration

The MCP surface exposes twelve tools — **none of which can write**. There is no `execute()` tool, no `query()` tool, no path from agent prompt to a write at your database, regardless of session state. The read-only guarantee is structural, not a session flag the agent can flip. `schemabrain serve` additionally pins `default_transaction_read_only=on` on the connection as belt-and-suspenders against a misconfigured downstream.

[Read-only by architecture →](docs/mechanism/read-only.mdx)

</td>
<td width="50%" valign="top">

### 2. PII-aware refusal at the `get_metric` tool boundary

Any `get_metric` touching a blocked PII category returns a `refused` envelope; the compiled SQL never runs and the refusal lands in `mcp_audit` as `status='refused'`, `refusal_reason='pii_blocked'`. `describe_entity` enforces the same policy at the column level — the agent still sees the entity and its non-PII columns, but blocked columns ship with `redacted=True` and the LLM-enriched description cleared. `schemabrain init` writes `--pii-block credential,payment_card,government_id` into the Claude Desktop snippet by default — the catastrophic-leak categories where no plausible aggregate-analytics use case exists. `--pii-block` **replaces** the set rather than extending it, so widen by listing the full target set (don't drop the catastrophic-leak defaults by accident):

```bash
schemabrain serve --pii-block credential,payment_card,government_id,contact,health
```

Twelve categories from GDPR, CCPA/CPRA, HIPAA, PCI DSS, ISO 27018 — tagged per-column at index time. Detection is column-name patterns + redacted-sample inspection — see [`schemabrain/pii/classifier.py`](schemabrain/pii/classifier.py) for the full rule set.

**Enforcement scope:** binding is at the `get_metric` compile path today. Lower-level tools (`describe_entity`, `resolve_join`, `describe_table`) surface the `redacted=True` flag + `pii_categories` as advisory metadata so the agent can self-regulate, but they don't refuse. Uniform SQL-layer enforcement against agent-emitted SQL ships in v2 — see [Where it's going](#where-its-going).

[PII taxonomy & propagation →](docs/mechanism/pii-taxonomy.mdx) · [Operations view →](docs/observability.md#pii-classification-alpha)

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 3. Tamper-evident audit log

Every tool call under `schemabrain serve` writes one row to an append-only `mcp_audit` table — PII categories per row, content-addressable fingerprints, sha256 hash chain. `audit verify` re-walks the chain and exits non-zero if any past row was rewritten. Detects post-hoc tampering by any process without write access to the audit table; streaming the chain to an external immutable store is on the v3 roadmap (`mcp_audit` is local SQLite today).

```bash
schemabrain audit verify   # exit 0 = chain clean
```

[Tamper-evident audit chain →](docs/mechanism/audit-chain.mdx) · [Operations view →](docs/observability.md#audit-log-alpha)

</td>
<td width="50%" valign="top">

### 4. Failure is a contract, not a string

Refused and degraded calls return a structured `recovery.suggested_args` block — not a message agents have to parse. PII blocks ship the entity name to retry; ambiguous time dimensions ship the candidate to pick; unknown joins ship the next tool to call. Agents act on the contract programmatically.

```json
{
  "status": "refused",
  "kind": "ambiguous_time_dimension",
  "recovery": {
    "suggested_tool": "get_metric",
    "suggested_args": {"time_dimension": "order.placed_at"}
  }
}
```

[Structured recovery →](docs/mechanism/structured-recovery.mdx) · [Trust signal →](docs/mechanism/trust-signal.mdx) · [Charter v1.2 reference →](docs/agent-ux-charter.md#3-errors-are-prompts-for-the-next-tool-call)

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 5. Compile path: definitions → parameterized SQL

Entities, metrics, and canonical joins compile to parameterized SQL SchemaBrain runs on its side. The agent sees rows + the SQL that was run — never arbitrary statements at your database. LLM-suggested definitions during `init` are reviewed and applied explicitly; nothing reaches the runtime store until you accept it.

[Build your semantic layer →](docs/semantic-layer.md)

</td>
<td width="50%" valign="top">

### 6. Pluggable into any agent loop

The same MCP stdio surface Claude Desktop sees is exposed to any host that speaks MCP — your own Anthropic, OpenAI, or LangGraph loop included. [`examples/anthropic_demo.py`](examples/anthropic_demo.py) is a ~250-LOC drop-in that wires Claude Haiku 4.5 to `schemabrain serve` and prints exactly which tools the agent chose to call.

```bash
python examples/anthropic_demo.py \
    --url-env DATABASE_URL \
    --question "..."
```

~$0.005–0.02 per run on Haiku 4.5, bounded by `--max-turns`.

[Anthropic SDK walkthrough →](docs/setup/manual.md#3-wire-your-own-agent-anthropic-sdk)

</td>
</tr>
</table>

---

## Observability dashboard

SchemaBrain ships an opt-in read-only dashboard for the same audit + PII + refusal data the firewall is already writing.

```bash
pip install "schemabrain[ui]"
schemabrain dashboard
# → http://127.0.0.1:7878
```

Three surfaces:

- **PII Ledger** — entity-by-category matrix; catastrophic-leak markers identify which entities will trip the default `--pii-block` policy.
- **Refusals** — every `refused` envelope with its recovery hint, so you can see exactly what the agent was blocked from doing.
- **Audit Viewer** — append-only `mcp_audit` chain with hash-verify cursor; click any row to see the compiled SQL, parameters, and PII categories.

The dashboard binds **`127.0.0.1` only** — there is no `--host` flag, by design. It's read-only and reads the same SQLite store `serve` writes to. No agent talks to it.

[Dashboard guide →](docs/dashboard/overview.mdx) · [PII Ledger →](docs/dashboard/pii-ledger.mdx) · [Refusals →](docs/dashboard/refusals.mdx) · [Audit Viewer →](docs/dashboard/audit-viewer.mdx)

---

## Works with

SchemaBrain speaks the [Model Context Protocol](https://modelcontextprotocol.io) over **stdio**. `schemabrain init --host <X>` writes first-party config for four MCP clients; everything else that speaks MCP stdio works via `--host manual` (prints the snippet, you paste).

### First-party wiring

`schemabrain init --host <X>` writes the MCP entry directly into the host's config file.

| Client | Setup guide | Config path |
|---|---|---|
| **Claude Desktop** | [`/setup/claude-desktop`](docs/setup/claude-desktop.md) | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`<br>Windows: `%APPDATA%\Claude\claude_desktop_config.json` |
| **Claude Code** | [`/setup/claude-code`](docs/setup/claude-code.md) | Shells out to `claude mcp add` |
| **Cursor** | [`/setup/cursor`](docs/setup/cursor.md) | `~/.cursor/mcp.json` |
| **Windsurf** | [`/setup/windsurf`](docs/setup/windsurf.md) | `~/.codeium/windsurf/mcp_config.json` |

### Any other MCP stdio host

`schemabrain init --host manual` prints the JSON entry to stdout — paste it into whatever host config you're using. Any client that launches a subprocess and speaks MCP stdio should work in principle; we have not exhaustively tested each. Common targets:

- **Zed** — full walkthrough at [`docs/setup/zed.md`](docs/setup/zed.md)
- **Codex CLI** (working path for ChatGPT users) — full walkthrough at [`docs/setup/codex.md`](docs/setup/codex.md)
- **Cline** (VS Code extension) — paste into the MCP server settings
- **Continue** — paste into `~/.continue/config.json`
- **Your own agent loop** — see [`examples/anthropic_demo.py`](examples/anthropic_demo.py) for a ~250-LOC Anthropic-SDK reference

The 12-tool surface, PII firewall, audit chain, and recovery contracts are transport-agnostic — any compliant stdio MCP client gets the same guarantees.

### Agent frameworks

The same stdio MCP surface is reachable from any framework that can spawn an MCP server. The Anthropic SDK path is first-party-tested; the others work in principle if the framework's MCP integration speaks stdio.

- **Anthropic SDK** — first-party walkthrough at [`docs/setup/manual.md`](docs/setup/manual.md#3-wire-your-own-agent-anthropic-sdk); reference loop at [`examples/anthropic_demo.py`](examples/anthropic_demo.py)
- **LangChain / LangGraph** — via [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters)
- **Pydantic AI** — via its [built-in MCP support](https://ai.pydantic.dev/mcp/)
- **CrewAI / AutoGen / Agno / custom loops** — any framework with a stdio MCP client works in principle; we have not tested each

We don't ship per-framework adapters; the framework's standard MCP client is sufficient.

### Not yet supported (cloud / HTTPS hosts)

SchemaBrain v0.5 ships stdio only — no HTTPS / SSE transport. Clients that require a cloud HTTPS endpoint do **not** work today:

- **ChatGPT Connectors** — see the [honest gap page](docs/setup/chatgpt.md) for workarounds and the v0.5+ roadmap
- **Hosted MCP gateways** — by design (local-first wedge; see [vs Querybear](docs/compare/querybear.mdx))

If you need ChatGPT support today, a community stdio→HTTPS bridge (`mcp-remote`, `mcp-proxy`, etc.) may work; we have not validated any specific bridge against the PII / audit / recovery semantics.

[Security posture for procurement / review →](docs/security.md)

---

## Sample session

Real Claude Desktop session against the bundled SaaS fixture (12 tables, 84 columns, 12 entities):

> **You:** Using SchemaBrain, compute our usage volume broken down by plan tier.
>
> **Claude:** *(calls `get_metric(name="usage_volume", group_by=["plan.title"])`. The metric anchors on `usage_event`; reaching `plan` would need a join that isn't in the schema, so it refuses:)*
>
> ```json
> { "status": "error",
>   "error": {
>     "kind": "unreachable_entity",
>     "message": "entity 'plan' is not reachable from metric anchor 'usage_event'; no canonical join exists. Run `schemabrain joins suggest` to surface candidate joins.",
>     "recovery": { "suggested_tool": "resolve_join",
>                   "suggested_args": { "entity_a": "usage_event", "entity_b": "plan" } } } }
> ```
>
> *(Claude confirms at the join layer — `resolve_join("usage_event", "plan")` returns `kind: no_canonical_join`. `usage_events` carries only `workspace_id`, never a `plan_id`, and no table bridges the two.)*
>
> Verdict up front: I can't compute usage-by-plan, and you shouldn't want me to fake it — there is no modeled relationship between usage events and plans in this schema. `usage_events` records a `workspace_id`, not a plan, and nothing joins the two. "Usage volume by plan tier" is unanswerable until that link exists — and I'm not going to invent it.
>
> What I **can** give you — contracted revenue by plan tier (this path fully resolves: `subscription_item → subscription → plan`, both `fk_constraint` joins, `confidence: HIGH`):
>
> ```sql
> SELECT p.title                                     AS plan_tier,
>        SUM(si.unit_price_cents * si.seats) / 100.0  AS revenue   -- prices stored in integer cents
> FROM   public.subscription_items si
> JOIN   public.subscriptions      s ON si.subscription_id = s.id
> JOIN   public.plans              p ON s.plan_id          = p.id
> GROUP BY p.title
> ORDER BY revenue DESC;
> ```
>
> | plan_tier  | revenue     |
> |------------|-------------|
> | Enterprise | $379,620.00 |
> | Pro        | $539.00     |
> | Free       | $54.00      |
>
> Two honest caveats:
> - **Contracted, not collected.** This is subscription line-item revenue (`seats × unit_price_cents`), not paid invoices. For billed totals, the `total_revenue` metric anchors on `invoices` instead.
> - **Integer cents.** `unit_price_cents` is stored as an integer; the `/ 100.0` converts to currency.

The differentiator is what *didn't* happen: most LLM-over-database tools, asked for usage-by-plan, would confidently emit `JOIN plans p ON usage_events.plan_id = p.id` against a `plan_id` column that doesn't exist. SchemaBrain refused — `get_metric` returned `kind: unreachable_entity` with `recovery.suggested_tool: resolve_join`, not prose. The agent **acted on the structured recovery contract programmatically** instead of fabricating a join. Refusal-not-fabrication is the safety mechanism, demonstrated live.

**Cost.** ~$0.001/column with Claude Haiku 4.5 + Sonnet 4.6 (Sonnet for the structured curation prompt). The bundled 12-table fixture (84 columns, 12 entities + 5 metrics + 8 joins) ships pre-curated, so the demo path applies it for **$0** — no API key. Indexing those 84 columns with LLM column descriptions measured **~$0.034**. The Pagila DVD-rental sample (87 columns after partition deduplication) runs for **$0.0299 in 105s**. Re-indexing an unchanged schema is **$0** — content-addressable fingerprinting skips the LLM call entirely.

To verify Claude's SQL is mechanically correct (and that flagged caveats are the actual data behavior), see [Validating SQL Claude generates](docs/setup/manual.md#6-validating-sql-claude-generates).

**Run this exact session yourself:** `schemabrain init` walks you to a wired Claude Desktop in one command; then ask Claude *"Using SchemaBrain, compute our usage volume broken down by plan tier."* and watch the refuse-then-pivot live.

---

## Where it's going

SchemaBrain is being built as the **SQL-boundary safety layer for AI agents** — the layer that parses what your agent is about to ask the database and refuses (or rewrites) before it runs.

That layer needs a semantic substrate underneath it. You can't refuse "this query touches PII" without knowing which columns are PII. You can't rewrite "join through this junction" without canonical-join definitions. You can't validate a metric without knowing its grain.

So the engineering order is **schema intelligence → semantic substrate → safety primitives.** The first two are shipped (v0.5 + v1); the third — `validate_query` for agent-emitted SQL and `execute` with hard caps — is the next major milestone. Today the product gives you PII-aware refusal at the `get_metric` boundary, structured recovery on every refused or degraded call, and tamper-evident audit — all running against parameterized SQL the agent never sees. If you need parse-before-execute over arbitrary agent-emitted SQL, track the roadmap.

---

## Roadmap

> The `v0.5` / `v1` / `v2` / `v3` labels are **roadmap milestone names**, not package versions. The package follows strict semver — `1.0.0` is reserved for an API that's been battle-tested by external users without a forced break. See [ADR-0003](docs/adr/0003-versioning-policy.md).

**v0.5 — schema intelligence (shipped):**
- Agent-UX charter v1.0 retrofit on existing tools + CI enforcement ✓
- Dev-UX foundations: rich progress UI, guided errors, `--dry-run` ✓
- Query log mining via `pg_stat_statements` (`schemabrain mine-queries`) ✓
- 5 physical-schema MCP tools including `get_example_queries` ✓

**v1 — semantic substrate (shipped):**
- Entities, metrics, canonical joins as first-class persisted definitions ✓
- LLM-suggested entity / metric / join definitions from FK graph + column descriptions ✓
- 5 semantic-layer MCP tools (`find_relevant_entities`, `list_entities`, `describe_entity`, `resolve_join`, `get_metric`) ✓
- Composite-expression measures — `SUM(unit_price * quantity)` over the same anchor table ✓
- Multi-hop canonical-join chains — BFS over the join graph with `via=` disambiguation ✓
- Drift detection (`schemabrain check`) ✓
- PII-aware refusal at the `get_metric` boundary ✓
- Tamper-evident audit log with sha256 chain ✓

**v1.x — engine breadth (in progress):**
- One additional engine: Snowflake / BigQuery / MySQL
- BIRD Mini-Dev automated eval harness
- Pre-built multi-platform Docker image on a public registry

**v2 — SQL-boundary safety wedge:**
- `validate_query` — agent-emitted SQL parsed and judged against policy before execution
- `execute` with hard caps — read-only role enforced at the database layer, statement timeouts, row caps, per-call cost guards
- Sub-query refusal with recovery — parse the SQL, identify the unsafe fragment, refuse just that fragment with a suggested rewrite

**v3 — multi-engine + control plane (commercial, gated on hosted demand):**
- Remaining engines (BigQuery / Snowflake / Redshift breadth)
- Learning loop from telemetry and reformulation patterns
- Hosted control plane with fleet-wide adversarial-signature aggregation

---

## Troubleshooting

The five most common first-run failures. Full troubleshooter in [`docs/setup/manual.md`](docs/setup/manual.md#5-troubleshooting).

- **`pip install schemabrain` gave me an older version.** Check `schemabrain --version`. If it doesn't match the [latest release](https://pypi.org/project/schemabrain/) your pip cache is stale — run `pip install --upgrade schemabrain`. `schemabrain init` writes the same version into the Claude Desktop snippet (`uvx schemabrain==<pin>`) so it stays reproducible across restarts; bump the pin in the snippet manually after upgrading via pip.
- **`init` reports `source unreachable`.** Postgres may not be ready on first run — wait a few seconds and re-run. For your own database, verify host, port, and credentials. Connection URLs in any form are accepted (`postgresql://`, `postgres://`, `postgresql+psycopg://`).
- **The first `init` or `schemabrain index` hangs for ~60 seconds.** Normal. The first index downloads the ONNX embedding model (~67 MB) and makes one LLM call per column. Subsequent runs are fast.
- **`init` fails at stage 6 "wire host".** Claude Desktop must be installed first — SchemaBrain writes into its config file, which doesn't exist until Claude Desktop has launched at least once.
- **Claude Desktop doesn't show SchemaBrain after restart.** Cmd+Q is required (close-window doesn't trigger a re-read of MCP config). Run `schemabrain doctor` to verify the config landed. If `doctor` says everything's good but Claude Desktop still doesn't see the tool, check `~/Library/Logs/Claude/mcp*.log`.

---

## Documentation

| Doc | What's inside |
|---|---|
| [`docs/setup.md`](docs/setup.md) | Activation wizard (recommended) — pick a host, run the wizard, ask the agent (~60s) |
| [`docs/setup/docker.md`](docs/setup/docker.md) | Docker install (image with embedding model baked in, no first-run download) |
| [`docs/setup/manual.md`](docs/setup/manual.md) | Manual `index`, mine-queries, logs config, troubleshooting, MCP Inspector, SQL-validation ladder |
| [`docs/first-5-queries.md`](docs/first-5-queries.md) | What to actually *do* after `init` — five queries that exercise read-only, PII refusal, audit chain, and structured recovery |
| [`docs/semantic-layer.md`](docs/semantic-layer.md) | Building entities, metrics (incl. composite expressions), canonical joins (incl. multi-hop), dbt import |
| [`docs/operations.md`](docs/operations.md) | `inspect`, `check` (drift), `index --dry-run`, Docker compose |
| [`docs/observability.md`](docs/observability.md) | `tail`, audit log, OTel export, PII classification |
| [`docs/reference/mcp-tools/`](docs/reference/mcp-tools/) | Full reference for all 12 MCP tools (overview + 12 per-tool pages) |
| [`docs/architecture.mdx`](docs/architecture.mdx) | Pipeline, retrieval contract, cache logic, cost model, eval |
| [`docs/dashboard/overview.mdx`](docs/dashboard/overview.mdx) | Read-only observability dashboard — PII ledger, refusals, audit viewer |
| [`docs/landscape.md`](docs/landscape.md) | Comparison vs Vanna / Atlan / dbt-mcp / WrenAI; "is this a semantic layer?" |
| [`docs/threat-model.md`](docs/threat-model.md) | Security model + boundaries |
| [`docs/adr/`](docs/adr/) | Architecture decision records (audit/PII taxonomy, store protocol, versioning policy, observability bus) |
| [`examples/`](examples/) | Copy-paste-ready MCP configs, headless agent loop, end-to-end ecommerce walkthrough |

---

## FAQ

**Does my data leave my machine?**
Only LLM-enriched column descriptions and the redacted sample values that feed them. Three regex passes (email, US SSN, credit-card-shaped digit runs) run on every sample before it leaves the profiler module — see [`schemabrain/profiler/stats.py`](schemabrain/profiler/stats.py). The Anthropic API call sends column metadata + redacted samples + sibling-column context — no raw rows. Embeddings are generated locally via `fastembed` (BAAI/bge-small-en-v1.5, ONNX, ~67 MB).

**What databases work today?**
Postgres 16+ (primary target) and SQLite (for development and demos). Adding Snowflake / BigQuery / MySQL is mostly a new `DataSource` implementation plus a profiler tweak — on the v1.x roadmap.

**Why MCP and not a REST API?**
The consumer is an agent, not a service. MCP standardizes tool registration, schema description, and request/response transport. Agents discover SchemaBrain natively and get its tool surface — no API wrapper, no SDK to maintain per language.

**Is this a semantic layer like Cube or dbt Semantic Layer?**
No — SchemaBrain is a SQL firewall built on a semantic-layer substrate. Entities, metrics, and canonical joins are first-class persisted definitions (`list_entities`, `describe_entity`, `resolve_join`, `get_metric`), but they exist to make the safety primitives possible — read-only-by-architecture, PII refusal, audit chain. The substrate is the means; the firewall is the headline. Full comparison vs Cube / dbt-mcp / Vanna / WrenAI in [`docs/landscape.md`](docs/landscape.md).

More questions answered in [`docs/setup/manual.md`](docs/setup/manual.md#5-troubleshooting) (why local embeddings, more troubleshooting).

---

## Contributors

<a href="https://github.com/Arun-kc/schemabrain/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Arun-kc/schemabrain" alt="Contributors to schemabrain" />
</a>

---

## Contributing & License

PRs welcome. The bar is high — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the test-first / 99%-coverage / conventional-commits / architecture-invariants checklist. CI enforces all of it.

Bugs and feature requests use the structured templates in `.github/ISSUE_TEMPLATE/`. Issues without a reproduction (bugs) or a clear underlying problem (features) get closed with a request to re-open with the right info.

[MIT](LICENSE).
