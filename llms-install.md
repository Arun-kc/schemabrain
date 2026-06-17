# Installing SchemaBrain in Cline

SchemaBrain is a **read-only** trust + intelligence layer between an AI agent and a
PostgreSQL database. It exposes **twelve read-only MCP tools** — the agent never writes
or runs raw SQL. PII and secret categories (credentials, payment cards, government IDs)
are refused *before* a query runs, and every call is recorded in a tamper-evident,
SHA-256 hash-chained audit log. Apache-2.0. PostgreSQL is the only source today.

There is no write tool and no raw-SQL tool, so it is safe to auto-approve all twelve tools.

## How SchemaBrain works (read this first)

SchemaBrain serves a **pre-built local index** of a database — it does not introspect
live tables at serve time. So setup is always two steps: **(1) build the index once**,
then **(2) point the MCP server at that store file.** The `serve` `--store-path` must be
**absolute**, because Cline launches the server from its own working directory, not from
wherever you built the store.

## Prerequisites

- `uv` (provides `uvx`). If it is missing, install it:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  (Alternatively `pipx install schemabrain` or `pip install schemabrain`, then use
  `schemabrain` in place of `uvx schemabrain` below.)

## Option A — Instant demo (recommended first; no database, no API key)

Stands up a working 12-tool server against bundled sample SaaS data so the install can be
verified immediately.

1. Build the demo store (no Docker, no API key). It is written to `~/.schemabrain/demo.db`:
   ```bash
   uvx schemabrain demo --showcase
   ```

2. Add this entry to Cline's `cline_mcp_settings.json`, merged into the existing
   `mcpServers` object. Replace `<HOME>` with the absolute path to the home directory
   (e.g. `/Users/you` or `/home/you`):
   ```json
   "schemabrain": {
     "command": "uvx",
     "args": [
       "schemabrain", "serve",
       "--source", "postgresql+psycopg://postgres:local@localhost:5433/postgres",
       "--store-path", "<HOME>/.schemabrain/demo.db"
     ],
     "type": "stdio"
   }
   ```
   The `--source` value is the demo store's pinned identifier; SchemaBrain reads the
   offline store and never connects to that address.

3. Reload Cline's MCP servers (Command Palette → **Developer: Reload Window** if the
   server does not appear). `schemabrain` should list **12 tools**. Verify by asking:

   > list the entities SchemaBrain knows about

## Option B — Your own PostgreSQL database

1. Index the database once. `--no-enrich` keeps it cost-free and requires no API key.
   Use an absolute `--store-path`:
   ```bash
   export SCHEMABRAIN_DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME"
   uvx schemabrain index --url-env SCHEMABRAIN_DATABASE_URL --store-path "<HOME>/schemabrain.db" --no-enrich
   ```
   (For LLM-assisted PII classification and entity/metric curation, drop `--no-enrich`
   and set `ANTHROPIC_API_KEY` — a default per-run cost cap applies. Or run
   `uvx schemabrain init --url-env SCHEMABRAIN_DATABASE_URL` for the guided wizard.)

2. Add this entry to `cline_mcp_settings.json` under `mcpServers`. The URL is passed via
   an env var so it never appears in argv:
   ```json
   "schemabrain": {
     "command": "uvx",
     "args": [
       "schemabrain", "serve",
       "--url-env", "SCHEMABRAIN_DATABASE_URL",
       "--store-path", "<HOME>/schemabrain.db"
     ],
     "env": {
       "SCHEMABRAIN_DATABASE_URL": "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME"
     },
     "type": "stdio"
   }
   ```

3. Reload Cline's MCP servers and ask:

   > list the entities SchemaBrain knows about

## Verifying without an agent (optional)

A no-API-key, mock-agent end-to-end smoke is available:
```bash
uvx schemabrain doctor --verify --host manual --store-path "<HOME>/.schemabrain/demo.db"
```

## Notes

- **`--store-path` must be absolute** — Cline launches the server from its own CWD.
- All twelve tools are read-only; there is no path from an agent prompt to a database write.
- The catastrophic PII floor (credentials, payment cards, government IDs) is refused by
  default with no configuration.
- Repo: https://github.com/Arun-kc/schemabrain · Docs: https://schemabrain.mintlify.app
