# SchemaBrain + Windsurf

> **60 seconds:** install SchemaBrain, run `schemabrain init`, restart Windsurf, ask Cascade about your database.

SchemaBrain is the SQL firewall between Windsurf's Cascade agent and your Postgres database — twelve read-only MCP tools, validated metrics, tamper-evident audit. Works on macOS, Linux, and Windows.

---

## Install

```bash
pip install schemabrain
schemabrain init --host windsurf
```

The wizard prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 7-table e-commerce fixture spins up in Docker; ~$0.03 to index). Press Enter to take the default (`2`).

The wizard then introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, then writes the MCP entry to `~/.codeium/windsurf/mcp_config.json` (Windsurf's global MCP config — the Codeium namespace persists from before Windsurf split out as a standalone IDE).

## Resulting config entry

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "uvx",
      "args": [
        "schemabrain==X.Y.Z",
        "serve",
        "--url-env", "SCHEMABRAIN_DATABASE_URL",
        "--store-path", "/Users/you/.schemabrain/store.db",
        "--pii-block", "credential,government_id,payment_card"
      ],
      "env": {
        "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://user:pass@host:5432/db"
      }
    }
  }
}
```

The JSON shape matches Claude Desktop's `mcpServers.{name}` map exactly — no extra fields. The `SCHEMABRAIN_DATABASE_URL` env-var key is the wizard's default, prefixed to avoid colliding with any app-level `DATABASE_URL` you already have in Windsurf's env.

## Restart Windsurf

Quit Windsurf fully and relaunch. The MCP config is only read on cold start.

## Ask Cascade

> list the entities SchemaBrain knows about

If Cascade calls `list_entities` and reports the entities curated during init, you're done. Otherwise run `schemabrain doctor --verify` for an end-to-end smoke test that doesn't require an Anthropic key.

**Next:** [First 5 Queries](../first-5-queries.md) walks you through exercising each load-bearing mechanism (read-only, PII refusal, audit chain, structured recovery) in ~10 minutes.

---

## What you get

- **12 MCP tools, none of which can write.** Full list in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** Defaults block `credential`, `payment_card`, `government_id`; widen with `--pii-block contact,health,...`. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
- **Tamper-evident audit chain.** Verify with `schemabrain audit verify`. Details in [`/mechanism/audit-chain`](../mechanism/audit-chain.md).
- **Structured recovery envelopes.** Refusals ship typed contracts Cascade can act on programmatically. Details in [`/mechanism/structured-recovery`](../mechanism/structured-recovery.md).

## Sample interaction

> **You:** What categories do our top 5 products by revenue belong to?
>
> **Cascade:** *(calls `find_relevant_entities("product revenue")` → `resolve_join` finds the canonical join path → `get_metric` returns the answer)*

When two parallel joins exist between the same pair of entities, the response disambiguates with a structured choice:

```json
{
  "status": "error",
  "error": {
    "kind": "ambiguous_join",
    "recovery": {
      "suggested_tool": "resolve_join",
      "suggested_args": {
        "entity_a": "order",
        "entity_b": "user",
        "name": "order_buyer"
      }
    }
  }
}
```

Cascade reads `recovery.suggested_tool` and `suggested_args.name`, calls `resolve_join` with the disambiguating canonical-join name, and continues. No guessing, no hallucinated SQL.

---

## Troubleshooting

- **Windsurf doesn't list `schemabrain` in its MCP UI** — confirm `~/.codeium/windsurf/mcp_config.json` exists and contains the entry. Check the MCP status in Windsurf's settings panel.
- **`uvx` not on PATH** — install with `pip install uv` (or `brew install uv`). Without `uvx`, the wizard falls back to the installed absolute path.
- **`postgresql://` URL fails with `ModuleNotFoundError`** — use `postgresql+psycopg://`. `init` auto-rewrites with a one-line confirmation.
- **`onnxruntime` install fails on Apple Silicon + Python 3.12** — downgrade to Python 3.11 or pass `--no-embed` to skip the embeddings layer.

Full setup reference: [`docs/setup.md`](../setup.md).
