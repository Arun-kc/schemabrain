---
title: "First 5 Queries"
description: "Five queries against your fresh SchemaBrain install that exercise each of the four load-bearing mechanisms — read-only tools, PII refusal, audit chain, structured recovery."
---

# First 5 Queries

> You've run `schemabrain init` and restarted your MCP host. This is what to actually *do* with it for the next 10 minutes. Five queries that exercise each of the four load-bearing mechanisms — read-only tools, PII-aware refusal, audit chain, structured recovery — plus a closing CLI step that proves what happened.

Run these in order against the bundled SaaS fixture (the default if you pressed Enter at the wizard's URL prompt). They work identically against your own Postgres — only the entity names change.

---

## Prerequisites

- `schemabrain init` completed successfully (Stage 7 reported a wired host).
- MCP host restarted cold (Cmd+Q on macOS, full quit on Windows, new terminal session on `claude-code`).
- A new conversation window open.

If any of those is uncertain, run `schemabrain doctor --verify` first — it smoke-tests the wiring without needing an Anthropic key. Details in [`/setup/claude-desktop`](/setup/claude-desktop#troubleshooting) and siblings.

---

## Query 1 — verify the wire

> **Ask Claude:** list the entities SchemaBrain knows about

**What happens.** Claude calls [`list_entities`](/reference/mcp-tools/list_entities). With the bundled fixture, the response is the pre-curated entity pack the wizard auto-applies on the demo path — no `ANTHROPIC_API_KEY` required:

| name | bound table | one-line description |
|---|---|---|
| `workspace` | `public.workspaces` | a tenant account — owns users, subscriptions, and billing |
| `user` | `public.users` | a workspace member account (login credential + contact details) |
| `plan` | `public.plans` | a subscription plan in the catalog (tier, price, seats, SLA) |
| `subscription` | `public.subscriptions` | a workspace's binding to a plan, with lifecycle timestamps |
| `subscription_item` | `public.subscription_items` | a per-seat line item on a subscription |
| `invoice` | `public.invoices` | a billing document issued to a workspace |
| `payment_method` | `public.payment_methods` | a card on file for a workspace (tokenized last-4, brand, expiry) |
| `billing_profile` | `public.billing_profiles` | a workspace's legal / tax identity used for invoicing |
| `api_key` | `public.api_keys` | a programmatic API credential issued to a workspace |
| `session` | `public.sessions` | an authenticated login session for a user |
| `usage_event` | `public.usage_events` | a metered telemetry event (api_call / login / export) |
| `support_ticket` | `public.support_tickets` | a customer-filed support request (free-text body) |

Against your own Postgres (not the demo), Stage 3 of `init` runs the LLM-driven entity suggester instead of the bundled pack — names + descriptions will vary, but the shape stays the same: one `EntitySummary` per inferred entity, each bound to a physical table.

**What it proves.** The MCP stdio transport is up, the local SQLite store has entities applied, and Claude is calling SchemaBrain tools instead of guessing at your schema. If Claude responds without calling a tool — that's an unwired host, not a SchemaBrain problem. Run `schemabrain doctor --verify`.

---

## Query 2 — see the semantic layer with PII tags

> **Ask Claude:** describe the user entity

**What happens.** Claude calls [`describe_entity(name="user")`](/reference/mcp-tools/describe_entity). The response lists every column on the bound physical table with its PII classification:

```json
{
  "name": "user",
  "qualified_table": "public.users",
  "identity": "id",
  "columns": [
    {"name": "id", "data_type": "bigint", "pii_categories": []},
    {"name": "workspace_id", "data_type": "bigint", "pii_categories": []},
    {"name": "email", "data_type": "text",
     "pii_categories": ["contact"], "redacted": false},
    {"name": "full_name", "data_type": "text",
     "pii_categories": ["contact"], "redacted": false},
    {"name": "password_hash", "data_type": "text",
     "pii_categories": ["credential"], "redacted": true},
    {"name": "role", "data_type": "text", "pii_categories": []},
    {"name": "created_at", "data_type": "timestamptz", "pii_categories": []}
  ]
}
```

(The full `EntityDetail` shape — including LLM-enriched descriptions, sample values, and `description_source` — is in [the MCP tool reference](/reference/mcp-tools/overview).)

**What it proves.** Columns are tagged at index time against the [12-category PII taxonomy](mechanism/pii-taxonomy.md). On a zero-config install, `--pii-block` defaults to `credential,government_id,payment_card` — the three catastrophic-leak categories. `password_hash` is tagged `credential` and `redacted: true` — the firewall will refuse any query that returns this column. `contact` is *tagged but not blocked* by default, so `redacted: false` on `email` is correct: the agent sees the tag as advisory metadata and can self-regulate even when policy doesn't refuse.

---

## Query 3 — run a metric (happy path)

> **Ask Claude:** what's our total revenue per month?

**What happens.** Claude calls [`list_metrics`](/reference/mcp-tools/list_metrics) to discover the vocabulary, finds `total_revenue` (entity `invoice`, `sum` of `invoice_total_cents`, time-dimension `invoice.issued_at`), then calls [`get_metric`](/reference/mcp-tools/get_metric):

```json
{
  "name": "total_revenue",
  "time_grain": "month"
}
```

The metric compiler emits SQL of roughly this shape (identifiers fully double-quoted, no ORDER BY unless the metric defines one):

```sql
SELECT date_trunc('month', "invoice"."issued_at") AS time_bucket,
       sum("invoice"."invoice_total_cents")        AS "total_revenue"
FROM   "public"."invoices" AS "invoice"
GROUP BY time_bucket
```

…executes against the source DB under `default_transaction_read_only=on`, and returns one row per month (totals in integer cents — a `total_revenue` of `613634` is `$6,136.34`). WHERE-clause values (`filter_predicates`) bind through SQLAlchemy parameters — the only operator-controlled string baked into the SQL is the validated metric / column / entity identifier set.

**What it proves.** The semantic-layer substrate works end-to-end. The agent didn't author SQL — it composed a structured tool call against an operator-defined metric. The compiled SQL is read-only at the session level, runs through a `NullPool` connection (no session-state leakage between calls), and any operator filter values flow through bound parameters. See [`/mechanism/read-only`](mechanism/read-only.md).

---

## Query 4 — see the firewall refuse (zero config)

> **Ask Claude:** break our revenue down by the card number on file for each workspace

**What happens.** Claude finds `total_revenue` (anchored on `invoice`) and tries to group it by `payment_method.card_number_last4`, reached via the bundled canonical join chain `invoice → workspace → payment_method` (`invoice_workspace` + `workspace_payment_methods`). That group-by column is tagged `payment_card` at index time. The metric compiler propagates PII tags through the joins, sees `payment_card` in the default blocked set, and refuses *before* the database is queried. The response is a structured envelope, not prose:

```json
{
  "status": "refused",
  "error": {
    "kind": "pii_blocked",
    "message": "get_metric refused: metric touches PII categories ['payment_card'] that this server policy blocks",
    "recovery": {
      "suggested_tool": "describe_entity",
      "suggested_args": {"name": "invoice"}
    },
    "pii_categories": ["payment_card"]
  }
}
```

`suggested_args.name` points Claude back at the metric's anchor to re-inspect the join path. Claude calls `describe_entity(name="payment_method")`, sees that `card_number_last4` is `redacted: true`, and either (a) drops the card grouping and retries against `card_brand` + `card_exp_*` only, or (b) reports back that the agent shouldn't expose card data and asks the operator to confirm intent. Either pivot is programmatic — **no human round-trip** to debug.

**What it proves.** This is the canonical-PII-propagation demo: PII tags **cross JOIN boundaries**. Even though the metric anchors on a non-PII table (`invoices.invoice_total_cents`), the compiler refused the *joined result* because the grouping reached `payment_methods.card_number_last4` two joins away. The database never saw the query. The refusal is a typed contract (one of [26 ErrorKind values](mechanism/structured-recovery.md)) with a `recovery.suggested_tool` the agent can branch on programmatically. See [`/mechanism/pii-taxonomy`](mechanism/pii-taxonomy.md) and [`/mechanism/structured-recovery`](mechanism/structured-recovery.md).

> **Try the credential variant too.** `Ask Claude: count users grouped by their password hash`. The bundled `users.password_hash` column is tagged `credential`, so `get_metric` refuses for the same reason — different category, same shape.

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

- **Add your own metric.** Drop a YAML under `./schemabrain/metrics/` and run `schemabrain metrics apply ./schemabrain/metrics/ --url-env DATABASE_URL`. Reference: [`docs/semantic-layer.md`](semantic-layer.md).
- **Stream the audit feed.** `schemabrain tail` shows every tool call live. Useful while you're tuning policy. Reference: [`docs/operations.md`](operations.md).
- **Wire a second host.** Same `init`, different `--host` flag. Reference: [`docs/setup/cursor`](setup/cursor.md), [`docs/setup/windsurf`](setup/windsurf.md), [`docs/setup/claude-code`](setup/claude-code.md).
- **Read the threat model.** If you're packaging SchemaBrain into a procurement review, [`docs/security.md`](security.md) is the summary; [`docs/threat-model.md`](threat-model.md) is the full walk-through.
