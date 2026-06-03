---
title: "Claude Desktop"
description: "60-second wiring: install SchemaBrain, run init, restart Claude Desktop with Cmd+Q, ask about your database."
---

# SchemaBrain + Claude Desktop

> **60 seconds:** install SchemaBrain, run `schemabrain init`, restart Claude Desktop with **Cmd+Q**, ask Claude about your database.

SchemaBrain is the trust and intelligence layer between Claude Desktop and your Postgres database — twelve read-only MCP tools, validated metrics, tamper-evident audit. Works on macOS and Windows; Claude Desktop has no Linux build today, so Linux users see [`/setup/claude-code`](claude-code.md) instead.

---

## Install

```bash
uvx schemabrain init --host claude-desktop
# or install first: pipx install schemabrain (or: pip install schemabrain)
```

The wizard takes ~45 seconds end-to-end on a warm cache. It prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 12-table SaaS fixture spins up in Docker; ~$0.03 to index). Just press Enter to take the default (`2`) and try the demo.

It then introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, and writes the MCP entry to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

## Restart Claude Desktop

**Quit fully with Cmd+Q (macOS) or via the system tray (Windows).** Closing the window is not enough — Claude Desktop only reads the MCP config on cold start. Relaunch.

## Ask Claude

> list the entities SchemaBrain knows about

If Claude calls `list_entities` and reports `user`, `order`, etc., you're done. If not, run `schemabrain doctor --verify` to smoke-test the wiring without an Anthropic key.

**Next:** [First 5 Queries](../first-5-queries.md) walks you through exercising each load-bearing mechanism (read-only, PII refusal, audit chain, structured recovery) in ~10 minutes.

---

## What you get

- **12 MCP tools, none of which can write.** Full list and propagation rules in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** SchemaBrain defaults to blocking `credential`, `payment_card`, and `government_id`. `--pii-block` **replaces** the set rather than extending it, so widen by listing the full target set, e.g. `--pii-block credential,payment_card,government_id,contact,health`. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Every tool call lands in an append-only `mcp_audit` table with a SHA256 chain hash. Verify with `schemabrain audit verify`. Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** When `get_metric` refuses or fails, the response is a typed contract (`recovery.suggested_tool`, `recovery.suggested_args`) Claude can act on programmatically. Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

## Sample refusal envelope

When Claude attempts a metric that touches a blocked PII category:

```json
{
  "status": "refused",
  "error": {
    "kind": "pii_blocked",
    "message": "get_metric refused: metric touches PII categories ['credential'] that this server policy blocks",
    "recovery": {
      "suggested_tool": "describe_entity",
      "suggested_args": {"name": "user"}
    },
    "pii_categories": ["credential"]
  }
}
```

Claude reads `recovery.suggested_tool`, pivots to `describe_entity` to enumerate non-PII columns, and re-tries without the blocked column. No human round-trip required.

---

## Troubleshooting

- **"Server disconnected" in Claude Desktop** — almost always a missed Cmd+Q. Quit fully and relaunch.
- **`postgresql://` URL fails with `ModuleNotFoundError`** — use `postgresql+psycopg://` (psycopg v3 scheme). `schemabrain init` auto-rewrites the bare scheme with a one-line confirmation.
- **`onnxruntime` install fails on Apple Silicon + Python 3.12** — downgrade to Python 3.11 (`pyenv install 3.11.10 && pyenv local 3.11.10`) or pass `--no-embed` to degrade semantic search to keyword-only.

Full setup reference: [`docs/setup.md`](../setup.md) (wizard) · [`docs/setup/manual.md`](manual.md) (manual flow, logs, troubleshooting).
