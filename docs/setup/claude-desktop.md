# SchemaBrain + Claude Desktop

> **60 seconds:** install SchemaBrain, run `schemabrain init`, restart Claude Desktop with **Cmd+Q**, ask Claude about your database.

SchemaBrain is the SQL firewall between Claude Desktop and your Postgres database — twelve read-only MCP tools, validated metrics, tamper-evident audit. Works on macOS and Windows; Claude Desktop has no Linux build today, so Linux users see [`/setup/claude-code`](claude-code.md) instead.

---

## Install

```bash
pip install schemabrain
export DATABASE_URL='postgresql+psycopg://user:pass@host:5432/db'
schemabrain init --host claude-desktop --url-env DATABASE_URL --env-var DATABASE_URL
```

The wizard takes ~45 seconds end-to-end on a warm cache: it introspects the schema, classifies columns for PII, optionally calls Anthropic to suggest entities/metrics/joins, and writes the MCP entry to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

At the first prompt, pick option `2` (the default — just press Enter) to use the bundled demo container: a 7-table e-commerce fixture spins up in Docker (~$0.03 to index).

## Restart Claude Desktop

**Quit fully with Cmd+Q (macOS) or via the system tray (Windows).** Closing the window is not enough — Claude Desktop only reads the MCP config on cold start. Relaunch.

## Ask Claude

> list the entities SchemaBrain knows about

If Claude calls `list_entities` and reports `user`, `order`, etc., you're done. If not, run `schemabrain doctor --verify` to smoke-test the wiring without an Anthropic key.

---

## What you get

- **12 MCP tools, none of which can write.** Full list and propagation rules in [`/mechanism/read-only`](../mechanism/read-only.md).
- **PII-aware refusal at the `get_metric` boundary.** SchemaBrain defaults to blocking `credential`, `payment_card`, and `government_id`; widen with `--pii-block contact,health,...` during init. Details in [`/mechanism/pii-taxonomy`](../mechanism/pii-taxonomy.md).
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

Full setup reference (all flags, wizard stages, manual mode): [`docs/setup.md`](../setup.md).
