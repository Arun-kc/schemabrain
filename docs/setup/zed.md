---
title: "Zed"
description: "Wire SchemaBrain into Zed's AI assistant via --host manual + paste — Zed is a stdio MCP client, same surface as Claude Code / Cursor."
---

# SchemaBrain + Zed

> **60 seconds:** install SchemaBrain, run `schemabrain init --host manual`, paste the printed snippet into Zed's context-server settings, restart Zed, ask the assistant about your database.

Zed is a stdio MCP client — its AI assistant launches MCP servers as subprocesses and speaks JSON-RPC over stdin/stdout, the same wire shape as Claude Code and Cursor. SchemaBrain doesn't ship a `--host zed` flag yet (Zed isn't in the wizard's choices tuple), so the working path is `--host manual` + paste-into-config.

---

## Install

```bash
pip install schemabrain
schemabrain init --host manual
```

The wizard prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 12-table SaaS fixture spins up in Docker; ~$0.03 to index). Press Enter to take the default (`2`).

It then introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, and prints a ready-to-paste JSON entry to stdout instead of writing into a host config. The block looks like:

```json
{
  "schemabrain": {
    "command": "uvx",
    "args": [
      "schemabrain==0.5.0",
      "serve",
      "--url-env", "SCHEMABRAIN_DATABASE_URL",
      "--store-path", "/ABSOLUTE/PATH/TO/schemabrain.db"
    ],
    "env": {
      "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://..."
    }
  }
}
```

## Paste into Zed's settings

Open Zed's `settings.json` (Cmd+Shift+P → "zed: open settings") and merge the printed entry into the `context_servers` block:

```json
{
  "context_servers": {
    "schemabrain": {
      "command": "uvx",
      "args": ["schemabrain==0.5.0", "serve", "--url-env", "SCHEMABRAIN_DATABASE_URL", "--store-path", "/ABSOLUTE/PATH/TO/schemabrain.db"],
      "env": {
        "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://..."
      }
    }
  }
}
```

Zed's MCP surface evolves quickly — consult the [Zed docs on MCP / context servers](https://zed.dev/docs) for the current key name and config schema. The `command` / `args` / `env` shape we emit is the standard MCP server spec, so it should drop into whichever key Zed's docs name today.

## Restart Zed and ask

Restart Zed (Cmd+Q on macOS / quit-and-relaunch on Linux — Zed only re-reads `settings.json` on cold start of the assistant subsystem). Open the assistant panel and ask:

> list the entities SchemaBrain knows about

If the assistant calls `list_entities` and reports `user`, `order`, etc., you're done. If not, run `schemabrain doctor --verify` to smoke-test the wiring without needing an Anthropic key.

**Next:** [First 5 Queries](../first-5-queries.md) exercises each load-bearing mechanism (read-only, PII refusal, audit chain, structured recovery) in ~10 minutes.

---

## What you get

- **12 MCP tools, none of which can write.** Full list in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** SchemaBrain defaults to blocking `credential`, `payment_card`, and `government_id`. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Every tool call lands in an append-only `mcp_audit` table with a SHA256 chain hash. Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** When `get_metric` refuses or fails, the response is a typed contract Zed's assistant can act on programmatically. Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

---

## Troubleshooting

- **Zed doesn't list `schemabrain` in the assistant panel** — confirm the entry is under the right key in `settings.json` (Zed's MCP key name has changed across versions; check the latest [Zed docs](https://zed.dev/docs)). Save and restart Zed.
- **Paths fail with "no such file"** — Zed runs the subprocess in a different working directory than your shell. Use absolute paths in `args` (run `realpath ./schemabrain.db` and `which schemabrain`).
- **`postgresql://` URL fails with `ModuleNotFoundError`** — use `postgresql+psycopg://` (psycopg v3 scheme). `schemabrain init` auto-rewrites the bare scheme during the wizard.
- **`onnxruntime` install fails on Apple Silicon + Python 3.12** — downgrade to Python 3.11 or pass `--no-embed` to degrade semantic search to keyword-only.

Full setup reference: [`docs/setup.md`](../setup.md) (wizard) · [`docs/setup/manual.md`](manual.md) (manual flow, logs, troubleshooting).
