# SchemaBrain + Claude Code

> **60 seconds:** install SchemaBrain, run `schemabrain init`, the wizard shells out to `claude mcp add` for you. Restart Claude Code, ask about your database.

SchemaBrain is the SQL firewall between Claude Code and your Postgres database — twelve read-only MCP tools, validated metrics, tamper-evident audit. Works on macOS, Linux, and Windows wherever the `claude` CLI is on PATH.

---

## Install

```bash
pip install schemabrain
schemabrain init --host claude-code
```

The wizard prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 7-table e-commerce fixture spins up in Docker; ~$0.03 to index). Press Enter to take the default (`2`).

The wizard then introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, then **shells out to `claude mcp add`** to register the server. We use the CLI rather than editing `~/.claude.json` directly because Anthropic's supported registration path validates the entry and is robust against schema changes.

## Verify

```bash
claude mcp list
# schemabrain  uvx schemabrain==X.Y.Z serve --url-env SCHEMABRAIN_DATABASE_URL --store-path ...
```

Then in a new Claude Code session:

> list the entities SchemaBrain knows about

If Claude calls `list_entities` and reports the entities curated during init, you're done. Otherwise run `schemabrain doctor --verify` for an end-to-end smoke test that doesn't require an Anthropic key.

**Next:** [First 5 Queries](../first-5-queries.md) walks you through exercising each load-bearing mechanism (read-only, PII refusal, audit chain, structured recovery) in ~10 minutes.

---

## If the shell-out failed

`schemabrain init` prints the exact `claude mcp add ...` command it tried. You can copy-paste and run it yourself:

```bash
claude mcp add \
  -e SCHEMABRAIN_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db \
  schemabrain -- \
  uvx schemabrain==X.Y.Z serve --url-env SCHEMABRAIN_DATABASE_URL --store-path ~/.schemabrain/store.db
```

The `--` separator is load-bearing — without it, Claude Code's parser would try to interpret `--url-env` as one of its own flags. The `SCHEMABRAIN_DATABASE_URL` env-var name is the wizard's default — it's prefixed to avoid colliding with any app-level `DATABASE_URL` you already have in the host's environment.

---

## What you get

- **12 MCP tools, none of which can write.** Full list in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** Defaults block `credential`, `payment_card`, `government_id`; widen with `--pii-block contact,health,...`. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Verify with `schemabrain audit verify`. Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** Refusals and errors ship typed contracts Claude can act on programmatically. Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

## Sample interaction

> **You:** Using SchemaBrain, what's our top customer by total spend?
>
> **Claude:** *(calls `find_relevant_entities("customer")` → picks `user` → `resolve_join(user, order)` → `resolve_join(order, order_item)` → `get_metric(name="customer_spend_total")`)*
>
> Top customer: Alice (`user.id=42`) with $4,219.50 across 18 orders. Path was fully resolved via FK constraints (`confidence: HIGH`).

The trust signal comes from the v1.2 envelope — see [`/mechanism/trust-signal`](../mechanism/trust-signal.md) for how Claude knows whether to qualify its answer.

---

## Troubleshooting

- **`claude: command not found`** — install Claude Code from https://claude.com/claude-code or use `--host manual` and paste the snippet into whatever MCP host you're using.
- **`postgresql://` fails with `ModuleNotFoundError`** — use `postgresql+psycopg://`. `init` auto-rewrites the scheme with a one-line confirmation.
- **`onnxruntime` install fails on Apple Silicon + Python 3.12** — downgrade to Python 3.11 or pass `--no-embed`.

Full setup reference: [`docs/setup.md`](../setup.md).
