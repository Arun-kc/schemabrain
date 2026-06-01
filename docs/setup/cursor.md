---
title: "Cursor"
description: "60-second wiring for Cursor's MCP integration. Auto-detected by the activation wizard."
---

# SchemaBrain + Cursor

> **60 seconds:** install SchemaBrain, run `schemabrain init`, restart Cursor, ask the agent about your database.

SchemaBrain is the SQL firewall between Cursor's agent and your Postgres database — twelve read-only MCP tools, validated metrics, tamper-evident audit. Works on macOS, Linux, and Windows.

---

## Install

```bash
pip install schemabrain
schemabrain init --host cursor
```

The wizard prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 12-table SaaS fixture spins up in Docker; ~$0.03 to index). Press Enter to take the default (`2`).

The wizard then introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, then writes the MCP entry to `~/.cursor/mcp.json` (Cursor's global config).

## Resulting config entry

`init` writes an entry shaped exactly like Cursor's documented schema:

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "uvx",
      "args": [
        "schemabrain==0.5.0",
        "serve",
        "--url-env", "SCHEMABRAIN_DATABASE_URL",
        "--store-path", "/Users/you/schemabrain.db",
        "--pii-block", "credential,government_id,payment_card"
      ],
      "env": {
        "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://user:pass@host:5432/db"
      },
      "type": "stdio"
    }
  }
}
```

The explicit `"type": "stdio"` is written per Cursor's documented schema. Cursor accepts entries without it (stdio is the default), but explicit is safer when their schema evolves. The `SCHEMABRAIN_DATABASE_URL` env-var key is the wizard's default — prefixed to avoid colliding with any app-level `DATABASE_URL` you already have in Cursor's env.

## Restart Cursor

Quit Cursor fully (Cmd+Q on macOS) and relaunch. Cursor only reads `mcp.json` on startup.

## Ask the agent

> list the entities SchemaBrain knows about

If Cursor's agent calls `list_entities` and reports the entities curated during init, you're done. Otherwise run `schemabrain doctor --verify` for an end-to-end smoke test.

**Next:** [First 5 Queries](../first-5-queries.md) walks you through exercising each load-bearing mechanism (read-only, PII refusal, audit chain, structured recovery) in ~10 minutes.

---

## Project-local vs global

`schemabrain init --host cursor` targets `~/.cursor/mcp.json` — install once, use across every project. If you want a per-project MCP entry, copy the same entry into `.cursor/mcp.json` at your project root; the wizard doesn't write project-local configs.

---

## What you get

- **12 MCP tools, none of which can write.** Full list in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** Defaults block `credential`, `payment_card`, `government_id`. `--pii-block` **replaces** the set rather than extending it, so widen by listing the full target set, e.g. `--pii-block credential,payment_card,government_id,contact,health`. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Verify with `schemabrain audit verify`. Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** Refusals ship typed contracts Cursor's agent can act on programmatically. Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

## Sample refusal

When the agent attempts a metric that touches a blocked PII category:

```json
{
  "status": "refused",
  "error": {
    "kind": "pii_blocked",
    "recovery": {
      "suggested_tool": "describe_entity",
      "suggested_args": {"name": "user"}
    },
    "pii_categories": ["credential"]
  }
}
```

The agent reads `recovery.suggested_tool`, pivots to `describe_entity` to find non-PII columns, retries. No human round-trip.

---

## Troubleshooting

- **Cursor doesn't list `schemabrain` in MCP** — confirm the path Cursor reads (`~/.cursor/mcp.json`) and the entry shape. Cursor's MCP indicator is in the bottom status bar.
- **`uvx` not on PATH** — install with `pip install uv` (or `brew install uv`). Without `uvx`, the wizard falls back to the installed absolute path.
- **`postgresql://` URL fails with `ModuleNotFoundError`** — use `postgresql+psycopg://`. `init` auto-rewrites with a one-line confirmation.

Full setup reference: [`docs/setup.md`](../setup.md) (wizard) · [`docs/setup/manual.md`](manual.md) (manual flow, logs, troubleshooting).
