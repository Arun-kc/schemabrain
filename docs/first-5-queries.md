---
title: "First 5 Queries"
description: "Five queries against your fresh SchemaBrain install that exercise each of the four load-bearing mechanisms — read-only tools, PII refusal, audit chain, structured recovery."
---

# First 5 Queries

> You've run `schemabrain init` and restarted your MCP host. This is what to actually *do* with it for the next 10 minutes. Five queries that exercise each of the four load-bearing mechanisms — read-only tools, PII-aware refusal, audit chain, structured recovery — plus a closing CLI step that proves what happened.

Run these in order against the bundled e-commerce fixture (the default if you pressed Enter at the wizard's URL prompt). They work identically against your own Postgres — only the entity names change.

---

## Prerequisites

- `schemabrain init` completed successfully (Stage 7 reported a wired host).
- MCP host restarted cold (Cmd+Q on macOS, full quit on Windows, new terminal session on `claude-code`).
- A new conversation window open.

If any of those is uncertain, run `schemabrain doctor --verify` first — it smoke-tests the wiring without needing an Anthropic key. Details in [`/setup/claude-desktop`](/setup/claude-desktop#troubleshooting) and siblings.

---

## Query 1 — verify the wire

> **Ask Claude:** list the entities SchemaBrain knows about

**What happens.** Claude calls [`list_entities`](/reference/mcp-tools/list_entities). With the bundled fixture, the response is three rows whose names and descriptions come from `init`'s LLM-driven entity suggestion (`schemabrain/entities/suggest.py`) — typically along the lines of:

| name (typical) | description (LLM-generated) |
|---|---|
| `customer` | …a registered shopper / user account |
| `order` | …one placed order tied to a customer |
| `product` | …a purchasable product with SKU and price |

Exact wording will vary slightly run-to-run because Stage 3 of `init` is an LLM call against your schema. What's fixed is the *shape*: three `EntitySummary` rows, each bound to one physical table (`public.users`, `public.orders`, `public.products`).

**What it proves.** The MCP stdio transport is up, the local SQLite store has entities applied, and Claude is calling SchemaBrain tools instead of guessing at your schema. If Claude responds without calling a tool — that's an unwired host, not a SchemaBrain problem. Run `schemabrain doctor --verify`.

---

## Query 2 — see the semantic layer with PII tags

> **Ask Claude:** describe the customer entity

**What happens.** Claude calls [`describe_entity(name="customer")`](/reference/mcp-tools/describe_entity) (substitute whatever name `list_entities` returned for the user/shopper entity). The response lists every column on the bound physical table with its PII classification:

```json
{
  "name": "customer",
  "qualified_table": "public.users",
  "identity": "id",
  "columns": [
    {"name": "id", "data_type": "bigint", "pii_categories": []},
    {"name": "email", "data_type": "text",
     "pii_categories": ["contact"], "redacted": false},
    {"name": "full_name", "data_type": "text",
     "pii_categories": ["contact"], "redacted": false},
    {"name": "created_at", "data_type": "timestamptz", "pii_categories": []}
  ]
}
```

(The full `EntityDetail` shape — including LLM-enriched descriptions, sample values, and `description_source` — is in [the MCP tool reference](/reference/mcp-tools/overview).)

**What it proves.** Columns are tagged at index time against the [12-category PII taxonomy](mechanism/pii-taxonomy.md). On a zero-config install, `--pii-block` defaults to `credential,government_id,payment_card` — the three catastrophic-leak categories. `contact` is *tagged but not blocked* by default, so `redacted: false` here is correct: the agent sees the tag as advisory metadata and can self-regulate even when policy doesn't refuse. Widen the block list with `--pii-block contact,...` in your host config when you're ready (see Query 4).

---

## Query 3 — run a metric (happy path)

> **Ask Claude:** what's the count of unique customers per month for the last 6 months?

**What happens.** Claude calls [`list_metrics`](/reference/mcp-tools/list_metrics) to discover the vocabulary, finds `customer_count` (entity `order`, `count_distinct` of `user_id`, time-dimension `order.placed_at`), then calls [`get_metric`](/reference/mcp-tools/get_metric):

```json
{
  "name": "customer_count",
  "time_grain": "month"
}
```

The metric compiler emits SQL of roughly this shape (identifiers fully double-quoted, no ORDER BY unless the metric defines one):

```sql
SELECT date_trunc('month', "order"."placed_at") AS time_bucket,
       count(DISTINCT "order"."user_id")        AS "customer_count"
FROM   "public"."orders" AS "order"
GROUP BY time_bucket
```

…executes against the source DB under `default_transaction_read_only=on`, and returns rows. WHERE-clause values (`filter_predicates`) bind through SQLAlchemy parameters — the only operator-controlled string baked into the SQL is the validated metric / column / entity identifier set.

**What it proves.** The semantic-layer substrate works end-to-end. The agent didn't author SQL — it composed a structured tool call against an operator-defined metric. The compiled SQL is read-only at the session level, runs through a `NullPool` connection (no session-state leakage between calls), and any operator filter values flow through bound parameters. See [`/mechanism/read-only`](mechanism/read-only.md).

---

## Query 4 — see the firewall refuse (opt-in)

This query requires a small policy tweak. The bundled e-commerce fixture has no `credential`, `payment_card`, or `government_id` columns, so the default catastrophic-only block list has nothing to refuse against. Widen the policy to also block `contact` (email, phone, full_name, address):

<Note>
  **Modifying the host policy:** Edit the arguments in your host configuration file (e.g. `claude_desktop_config.json`, `mcp.json`, or `mcp_config.json`) to include the `contact` category in the `--pii-block` flag:
  
  ```diff
  - "args": ["serve", "--store-path", "...", "--pii-block", "credential,government_id,payment_card"]
  + "args": ["serve", "--store-path", "...", "--pii-block", "contact,credential,government_id,payment_card"]
  ```
</Note>

<Warning>
  **Relaunch Required:** Quit and relaunch your MCP host fully (**Cmd+Q** on macOS) so it reads the new `--pii-block` configurations on startup.
</Warning>

Then ask:

> **Ask Claude:** show me unique customers per month, grouped by their email

**What happens.** Claude calls `get_metric` with `group_by: ["customer.email"]` (resolving through the FK-mined canonical join between `order` and `customer` that `init` Stage 5 created). The metric compiler propagates PII tags through the `group_by` surface, sees `contact` in the blocked set, and refuses *before* the database is queried. The response is a structured envelope, not prose:

```json
{
  "status": "refused",
  "error": {
    "kind": "pii_blocked",
    "message": "get_metric refused: metric touches PII categories ['contact'] that this server policy blocks",
    "recovery": {
      "suggested_tool": "describe_entity",
      "suggested_args": {"name": "order"}
    },
    "pii_categories": ["contact"]
  }
}
```

The `suggested_args.name` is the **metric's anchor entity** (`order` for `customer_count`), not the entity that owned the offending column. So Claude calls `describe_entity(name="order")` first, finds no `contact`-tagged columns there, then chases the join chain via `list_entities` → `describe_entity(name="customer")` to land on `id` / `created_at` as safe `group_by` candidates, and retries. The pivot takes a couple of hops, but it's all programmatic — **no human round-trip**.

**What it proves.** PII enforcement is a *compile-time* refusal, not a post-hoc filter — the database never sees the query. The refusal is a typed contract (one of [26 ErrorKind values](mechanism/structured-recovery.md)) with a `recovery.suggested_tool` the agent can branch on programmatically. See [`/mechanism/pii-taxonomy`](mechanism/pii-taxonomy.md) and [`/mechanism/structured-recovery`](mechanism/structured-recovery.md).

> **Note on the bundled fixture.** Because the demo e-commerce schema has no `credential`/`payment_card`/`government_id` columns, the catastrophic-leak default never visibly refuses anything against this dataset. Point SchemaBrain at a real schema with `password_hash` / `card_number` / `ssn` columns and the refusal fires on zero-config installs without widening anything.

---

## Query 5 — verify the audit chain

This one is CLI-side, not agent-side. Open a terminal:

```bash
schemabrain audit verify
```

**What happens.** SchemaBrain re-walks the SHA256 chain across every `mcp_audit` row written by Queries 1–4. Each row carries `chain_hash[N] = sha256(chain_hash[N-1] || canonical(row[N]))`. The verifier exits:

- **`0`** — chain intact, no row rewritten since it was committed.
- **`1`** — mismatch found; the row index that broke the chain is reported.
- **`2`** — operational error (DB missing, schema mismatch, IO failure).

For deeper inspection, `schemabrain audit list --limit 10` shows the last ten tool calls with their `tool_name`, `status`, `pii_categories`, and `chain_hash` columns.

**What it proves.** Every agent tool call is captured in an append-only table guarded by SQL triggers (`mcp_audit_no_update`, `mcp_audit_no_delete`) at the SQLite layer. Tampering with any past row breaks the chain at that row and every later one. The chain head can be persisted externally (post-`verify` cron) to detect even a full-table rewrite. See [`/mechanism/audit-chain`](mechanism/audit-chain.md).

---

## What you just exercised

| Query | Mechanism |
|---|---|
| Q1 — list entities | [Read-only by architecture](mechanism/read-only.md) — agent composed a structured call, not SQL |
| Q2 — describe entity | [PII taxonomy](mechanism/pii-taxonomy.md) — 12-category tagging at index time, surfaced as advisory metadata |
| Q3 — get metric | [Read-only by architecture](mechanism/read-only.md) + [trust signal](mechanism/trust-signal.md) — operator-validated metric, parameterized SQL, `default_transaction_read_only=on` |
| Q4 — refused metric | [PII propagation](mechanism/pii-taxonomy.md#3-propagation-through-joins-and-aggregations--the-compiler-layer-mechanism) + [structured recovery](mechanism/structured-recovery.md) — compile-time refusal, typed envelope, agent self-pivots |
| Q5 — audit verify | [Tamper-evident audit chain](mechanism/audit-chain.md) — SHA256 chain, append-only triggers, exit-code contract |

You've seen all four load-bearing mechanisms in ~10 minutes against a real database.

---

## Where to next

- **Add your own metric.** Drop a YAML in `~/.schemabrain/metrics/` and run `schemabrain metrics apply`. Reference: [`docs/semantic-layer.md`](semantic-layer.md).
- **Stream the audit feed.** `schemabrain tail` shows every tool call live. Useful while you're tuning policy. Reference: [`docs/operations.md`](operations.md).
- **Wire a second host.** Same `init`, different `--host` flag. Reference: [`docs/setup/cursor`](setup/cursor.md), [`docs/setup/windsurf`](setup/windsurf.md), [`docs/setup/claude-code`](setup/claude-code.md).
- **Read the threat model.** If you're packaging SchemaBrain into a procurement review, [`docs/security.md`](security.md) is the summary; [`docs/threat-model.md`](threat-model.md) is the full walk-through.
