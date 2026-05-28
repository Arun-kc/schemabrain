---
title: "Codex CLI"
description: "Wire SchemaBrain into OpenAI's Codex CLI via --host manual + paste — Codex is a local stdio MCP client, the working path for ChatGPT users today."
---

# SchemaBrain + Codex CLI

> **60 seconds:** install SchemaBrain, run `schemabrain init --host manual`, paste the printed snippet into Codex's MCP config, restart Codex, ask about your database.

OpenAI's Codex CLI is a local stdio MCP client, much like Claude Code. It can speak to SchemaBrain over stdin/stdout the same way our other supported hosts do. SchemaBrain doesn't ship a `--host codex` flag yet, so the working path is `--host manual` + paste-into-config.

This page is the practical answer for ChatGPT users — for the broader transport-gap context (why there's no first-party ChatGPT integration today), see the [ChatGPT page](chatgpt.md).

---

## Install

```bash
pip install schemabrain
schemabrain init --host manual
```

The wizard prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 7-table e-commerce fixture spins up in Docker; ~$0.03 to index). Press Enter to take the default (`2`).

It then introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, and prints a ready-to-paste JSON entry to stdout. The block looks like:

```json
{
  "schemabrain": {
    "command": "uvx",
    "args": [
      "schemabrain==0.4.0",
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

## Paste into Codex's MCP config

Codex CLI reads MCP server registrations from its own config — consult [Codex's docs](https://github.com/openai/codex) for the current path and key name (the location has shifted across releases). The `command` / `args` / `env` shape we emit is the standard MCP server spec, so it drops into whichever block Codex's docs name today.

## Restart Codex and ask

Quit and relaunch the Codex CLI so it re-reads its MCP config, then in a session ask:

> list the entities SchemaBrain knows about

If Codex's agent calls `list_entities` and reports `user`, `order`, etc., you're done. If not, run `schemabrain doctor --verify` to smoke-test the SchemaBrain wiring without needing an Anthropic key.

**Next:** [First 5 Queries](../first-5-queries.md) exercises each load-bearing mechanism (read-only, PII refusal, audit chain, structured recovery) in ~10 minutes.

---

## What you get

- **12 MCP tools, none of which can write.** Full list in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** SchemaBrain defaults to blocking `credential`, `payment_card`, and `government_id`. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Every tool call lands in an append-only `mcp_audit` table with a SHA256 chain hash. Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** When `get_metric` refuses or fails, the response is a typed contract Codex's agent can act on programmatically. Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

---

## What Codex CLI is NOT

- **Not the ChatGPT cloud integration.** ChatGPT's MCP support (Connectors) runs in OpenAI's cloud and expects HTTPS / SSE transports — SchemaBrain doesn't yet expose those (v0.5+ roadmap). The Codex CLI path described here runs SchemaBrain locally on your machine as a subprocess. See [ChatGPT](chatgpt.md) for the full transport-gap explanation.
- **Not the same as Claude Code.** Codex and Claude Code are separate CLIs from different vendors. Each has its own MCP config format; consult the respective vendor docs.

---

## Troubleshooting

- **Codex doesn't list `schemabrain` after restart** — confirm the entry is under the MCP key Codex's current version expects (the key name has shifted; check the latest [Codex docs](https://github.com/openai/codex)).
- **Paths fail with "no such file"** — Codex runs the subprocess in a different working directory than your shell. Use absolute paths in `args` (run `realpath ./schemabrain.db` and `which schemabrain`).
- **`postgresql://` URL fails with `ModuleNotFoundError`** — use `postgresql+psycopg://` (psycopg v3 scheme). `schemabrain init` auto-rewrites the bare scheme during the wizard.
- **`onnxruntime` install fails on Apple Silicon + Python 3.12** — downgrade to Python 3.11 or pass `--no-embed` to degrade semantic search to keyword-only.

Full setup reference: [`docs/setup.md`](../setup.md) (wizard) · [`docs/setup/manual.md`](manual.md) (manual flow, logs, troubleshooting).
