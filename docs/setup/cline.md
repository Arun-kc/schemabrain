---
title: "Cline"
description: "Wire SchemaBrain into Cline (VS Code) with the manual MCP snippet — already in Cline's mcpServers shape, all twelve read-only tools."
---

# SchemaBrain + Cline

> **~1 minute:** install SchemaBrain, run `schemabrain init --host manual`, paste the printed block into Cline's MCP settings, ask the agent about your database.

SchemaBrain is the trust and intelligence layer between Cline's agent and your Postgres database — twelve read-only MCP tools, validated metrics, tamper-evident audit. Cline isn't auto-wired by the activation wizard (there's no `--host cline`), so you wire it with the `--host manual` snippet — which `init` prints in Cline's exact `mcpServers` shape, ready to paste. Works on macOS, Linux, and Windows.

---

## 1. Install and generate the snippet

```bash
uvx schemabrain init --host manual
# or install first: pipx install schemabrain (or: pip install schemabrain)
```

The wizard prompts you to pick **1. Connect my own Postgres** (paste a `postgresql+psycopg://...` URL) or **2. Try with sample data** (a 12-table SaaS fixture spins up in Docker; ~$0.03 to index). It introspects the schema, classifies columns for PII, optionally curates entities/metrics/joins, and — because you passed `--host manual` — **prints the MCP entry to stdout instead of writing a host file.** The printed block is already wrapped in `mcpServers`, exactly the shape Cline expects:

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "uvx",
      "args": [
        "schemabrain==0.6.0",
        "serve",
        "--url-env", "SCHEMABRAIN_DATABASE_URL",
        "--store-path", "/Users/you/schemabrain.db",
        "--pii-block", "credential,government_id,payment_card"
      ],
      "env": {
        "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://user:pass@host:5432/db"
      }
    }
  }
}
```

Copy **your** printed block — the store path, env URL, and version pin are filled in for your install, so don't copy the example above verbatim. The `schemabrain==<version>` pin keeps restarts reproducible; if `uvx` isn't on your PATH, `init` emits the absolute path to your installed `schemabrain` entry point instead (still valid, just machine-specific).

## 2. Open Cline's MCP settings

In VS Code:

1. Click the **Cline** icon in the sidebar.
2. Click the **MCP Servers** icon (the stacked-server icon) at the top of the Cline panel.
3. In the **Configure** tab, click **Configure MCP Servers** — this opens Cline's `cline_mcp_settings.json`.

## 3. Paste the entry

Paste the block from step 1. If `cline_mcp_settings.json` already lists other servers, merge just the `schemabrain` key into the existing `mcpServers` object rather than replacing the file:

```json
{
  "mcpServers": {
    "some-existing-server": { "...": "..." },
    "schemabrain": {
      "command": "uvx",
      "args": ["schemabrain==0.6.0", "serve", "--url-env", "SCHEMABRAIN_DATABASE_URL", "--store-path", "/Users/you/schemabrain.db", "--pii-block", "credential,government_id,payment_card"],
      "env": { "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://user:pass@host:5432/db" }
    }
  }
}
```

Save the file. Cline reloads its MCP servers when the settings change; if SchemaBrain doesn't appear in the **MCP Servers** panel, reload the window (Command Palette → **Developer: Reload Window**) — no full app restart, and nothing macOS-specific.

> **Optional — auto-approve the read-only tools.** All twelve SchemaBrain tools are read-only (there is no write tool and no raw-SQL tool), so it's safe to enable auto-approve for them from Cline's **MCP Servers** panel if you'd rather not approve each call by hand. The PII firewall and the tamper-evident audit log still apply to every call.

## 4. Ask the agent

> list the entities SchemaBrain knows about

If Cline calls `list_entities` and reports the entities curated during `init`, you're done. Otherwise run `schemabrain doctor --verify` for an end-to-end smoke test that doesn't need an Anthropic key, and confirm the `--store-path` and `SCHEMABRAIN_DATABASE_URL` in your pasted entry match what `init` printed.
