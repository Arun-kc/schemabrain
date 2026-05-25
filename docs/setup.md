# SchemaBrain — Setup

Two paths from "I have a Postgres database" to "an AI agent can answer questions about it":

1. **MCP client (Claude Desktop or Cursor)** — for everyday use; click into the chat and ask questions.
2. **Anthropic SDK demo** — for verifying the install end-to-end without Claude Desktop, and for adapting SchemaBrain into your own agent code.

The recommended path is the activation wizard (`schemabrain init`). It runs the source check, indexer, entity suggestion, and host wiring in one command. The manual `index` flow below still works and is the right choice for power users who want explicit control over each step.

## 0. Activation wizard (recommended)

```bash
# Install (in a venv)
pip install schemabrain

# Or from source if you want to hack on it:
#   git clone https://github.com/Arun-kc/schemabrain && cd schemabrain
#   uv sync --extra dev
# (source-install users prefix the runtime commands below with `uv run`)

# Put the connection string in an env var so the password never lands
# in shell history, `ps`, or journald. SchemaBrain reads it via
# --url-env. URL MUST use the postgresql+psycopg:// scheme (psycopg v3;
# the bare postgresql:// scheme fails with ModuleNotFoundError).
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname"

# Optional: an Anthropic key unlocks the LLM-driven stages
# (entities = stage 3, metrics = stage 4). Without it those stages
# skip gracefully and you can curate later via
# `schemabrain entities suggest --apply` and
# `schemabrain metrics suggest --apply`. Stage 5 (joins) is
# deterministic and runs regardless.
export ANTHROPIC_API_KEY=sk-ant-...

schemabrain init --url-env DATABASE_URL --store-path ./schemabrain.db
```

The wizard runs seven stages:

1. **Source check** — validates URL reachable + read-only on Postgres. Auto-detects a dbt manifest from `$DBT_PROJECT_DIR/target/manifest.json` or by walking up from the cwd for a `dbt_project.yml`. When found, stages 3 and 4 route through the dbt importer instead of the LLM. Force a manifest with `--from-dbt PATH`.
2. **Index schema** — DDL introspection into `./schemabrain.db`. Cost-free by default; `--enrich` opts in to LLM column descriptions ($0.10–$2.00 for a 50-table schema).
3. **Curate entities** — Claude Sonnet 4.6 proposes domain entities (or dbt manifest is the source of truth when detected). Soft-skips if `ANTHROPIC_API_KEY` is absent. Cap spend with `--entities-max-cost-usd N`. Opt out with `--no-entities`.
4. **Curate metrics** — Claude Sonnet 4.6 proposes aggregations anchored on the curated entities (or dbt metrics are imported). Cap spend with `--metrics-max-cost-usd N`. Opt out with `--no-metrics`.
5. **Curate joins** — mines FK constraints + `pg_stat_statements` query log to surface canonical joins. Deterministic — no LLM call, no cost cap. Opt out with `--no-joins`.
6. **Wire host** — writes `schemabrain` into Claude Desktop's `mcpServers` block. Use `--host claude-code` for Claude Code (`claude mcp add`) or `--print-only` for any other host (paste-ready snippet to stdout).
7. **Next step** — prints what to ask the agent first.

Stages 3, 4, and 5 are best-effort: a failure records the issue and prints a guided next step, but doesn't abort the wizard. Stages 1, 2, 6, and 7 abort on failure.

Before each LLM-driven stage (entities + metrics), the wizard pauses for Enter-to-continue with the cost cap formatted in the prompt. Skip the pause in scripted runs with `--skip-llm-confirm`; the full superset `--yes` skips both the LLM pause and the host-overwrite prompt. The pause auto-suppresses in non-TTY environments (CI, pytest).

Re-runs are idempotent: every stage auto-skips when the work is already done. Use `--skip-index` to opt out of stage 2, `--no-entities` / `--no-metrics` / `--no-joins` to opt out of stages 3 / 4 / 5 individually.

## 0a. Manual flow (advanced)

If you want explicit control over each step — or you're scripting individual phases — the underlying commands still work:

```bash
# Index only (no host wiring, no entity suggestion)
schemabrain index --url-env DATABASE_URL --store-path ./schemabrain.db

# Or with LLM enrichment + ANTHROPIC_API_KEY:
# schemabrain index --url-env DATABASE_URL --store-path ./schemabrain.db
```

> **Legacy path:** the older `schemabrain index "postgresql+psycopg://..."` form
> still works for backwards compatibility, but emits a deprecation warning when
> the URL contains a password. New scripts should use `--url-env`.

The index step:
- Reflects every user-visible table.
- Generates one LLM-written column description per column (Claude Haiku 4.5; ~$0.0003/column at typical schema density) — only when run with enrichment enabled.
- Embeds each description locally with `BAAI/bge-small-en-v1.5` (~67 MB ONNX, ~10 ms/column warm).
- Persists everything to `./schemabrain.db`.

Re-running `index` against an unchanged schema is a **no-op** — 0 LLM calls, 0 embedder calls, ~0.1 s. Schema-changed tables are re-enriched + re-embedded selectively.

For cost-free dry runs (no LLM, no embeddings), pass `--no-enrich`.

## 0b. Docker (alternative install)

The published Docker image bundles the runtime, all dependencies, and the
local embedding model (`BAAI/bge-small-en-v1.5`, baked at build time so
the first `serve` does not download). Image is published to GitHub
Container Registry at `ghcr.io/arun-kc/schemabrain` for `linux/amd64`
and `linux/arm64`.

### Quick index against a Postgres source

```bash
# Persist the SQLite store and the events log to ~/.schemabrain on the
# host. The container writes there as a non-root user (uid 1000); the
# directory must be writable by that uid. Either chown the directory
# to uid 1000 (`sudo chown 1000:1000 ~/.schemabrain`) or run the
# container with `--user $(id -u)` to match your own uid.
mkdir -p ~/.schemabrain

# Export the URL first so the password never lands in shell history or
# in argv. Same discipline as the non-Docker setup. `-e DATABASE_URL`
# without a value passes the env var through to the container.
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname"

# Run `schemabrain index` with the connection URL passed via env var.
# `-i` is not needed for index (no stdio); it IS needed for serve.
docker run --rm \
    -e DATABASE_URL \
    -e ANTHROPIC_API_KEY \
    -v ~/.schemabrain:/data \
    ghcr.io/arun-kc/schemabrain:latest \
    index --url-env DATABASE_URL --store-path /data/store.db
```

### Claude Desktop config (containerised serve)

The `serve` subcommand needs `-i` (interactive stdio) so Claude Desktop
can talk to it. Drop the following into your
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS
path; the file lives next to other Claude config on Linux / Windows):

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "DATABASE_URL",
        "-v", "/Users/YOU/.schemabrain:/data",
        "ghcr.io/arun-kc/schemabrain:latest",
        "serve", "--url-env", "DATABASE_URL", "--store-path", "/data/store.db"
      ],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://user:pass@host:5432/dbname"
      }
    }
  }
}
```

Notes:

- Replace `/Users/YOU/.schemabrain` with your real home directory. Claude
  Desktop does not expand `~` inside config arguments.
- The `env` block carries the password into the container as a
  Docker-side environment variable. The URL never lands in argv on the
  host or in argv inside the container; `--url-env DATABASE_URL` reads
  it from the in-container env. This is the same discipline as the
  non-Docker setup.
- The mounted store volume (`-v .../.schemabrain:/data`) persists across
  container restarts. Without it, every `serve` rebuilds the in-memory
  retriever from zero and you lose `mine-queries` history.
- The image runs as uid 1000. If you indexed natively before switching
  to Docker, `chown -R 1000:1000 ~/.schemabrain` once so the container
  can read the store.

### Tags

| Tag | Meaning |
|---|---|
| `:latest` | Latest published release (PyPI publish + Docker push together) |
| `:0.4.0` | A specific version |
| `:0.4` | The latest patch in the 0.4 minor line |

For production-style pinning, use a specific patch (`:0.4.0`) rather
than `:latest`.

## 0.5. (Optional) Mine observed queries

`get_example_queries` returns SQL agents (and humans) have actually
run against your tables. To populate it, run `mine-queries` once
(or on a schedule):

```bash
schemabrain mine-queries \
    --url-env DATABASE_URL \
    --store-path ./schemabrain.db
```

Requires the `pg_stat_statements` extension on the source database:

```sql
-- One-time, as a superuser, in postgresql.conf:
--   shared_preload_libraries = 'pg_stat_statements'
-- then restart Postgres.
CREATE EXTENSION pg_stat_statements;
```

The mining role needs read access to the `pg_stat_statements` view
(superusers always have it; non-super roles need `pg_read_all_stats`).
If the extension or grant is missing, `mine-queries` exits cleanly
with an actionable message — no row is written, the store stays
intact, and `get_example_queries` keeps returning `status: empty`
with a recovery hint pointed at this section.

## Path 1 — Claude Desktop (or Cursor)

Add SchemaBrain to Claude Desktop's MCP server config:

```bash
# macOS path; Windows uses %APPDATA%\Claude\claude_desktop_config.json
$EDITOR ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

Paste (or merge) this block; replace the placeholders:

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "/ABSOLUTE/PATH/TO/.venv/bin/schemabrain",
      "args": [
        "serve",
        "--url-env",
        "DATABASE_URL",
        "--store-path",
        "/ABSOLUTE/PATH/TO/schemabrain.db"
      ],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME"
      }
    }
  }
}
```

Putting the URL in `env` (instead of an argv string) keeps the password out of `ps` output and any logs that capture process command lines. The legacy `"--source", "<url>"` form still works but emits a deprecation warning when the URL contains a password.

A copy-paste-ready template lives at `examples/claude_desktop_config.example.json`.

**Important: paths must be absolute.** Claude Desktop runs your config in a different working directory than your shell, so relative paths and `~` won't resolve. Run `realpath ./schemabrain.db` and `which schemabrain` (with your `pip install`'d venv active, or your source-install venv via `uv sync`) to get the absolute paths.

Restart Claude Desktop. In a new conversation you should see the `schemabrain` MCP server listed in the tool tray. Try a question like "Which tables in my database describe orders?" — Claude will call `find_relevant_tables` and `describe_table` to answer.

**Cursor uses a near-identical `mcpServers` block** — with one Cursor-specific addition: a `"type": "stdio"` field on each server entry (required per Cursor's official docs, even though the IDE has historically been lenient about omitting it). Paste the template from `examples/cursor_mcp_config.example.json` into `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` in your project root (project-scoped, takes precedence). Restart Cursor; the `schemabrain` server appears in the MCP tools list. Same absolute-path rule applies — Cursor doesn't run your config in your shell's working directory.

## Path 2 — Anthropic SDK demo (no Claude Desktop required)

The demo script at `examples/anthropic_demo.py` spawns `schemabrain serve` over stdio, drives Claude Haiku via the Anthropic SDK's standard tool-use loop, and prints the conversation transcript.

The example script ships in the source repo but not in the PyPI wheel. If you installed via `pip`, grab the script first:

```bash
curl -O https://raw.githubusercontent.com/Arun-kc/schemabrain/main/examples/anthropic_demo.py
pip install anthropic  # also needs the Anthropic SDK
```

Then run it (source-install users prefix with `uv run`):

```bash
export ANTHROPIC_API_KEY=sk-ant-...

export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname"

python anthropic_demo.py \
    --source "$DATABASE_URL" \
    --store-path ./schemabrain.db \
    --question "Where do we store customer order totals?"
```

You'll see something like:

```
[discovered tools] ['find_relevant_tables', 'describe_table']

[user] Where do we store customer order totals?

[tool call] find_relevant_tables({"query": "customer order totals", "limit": 5})
[tool result] is_error=False, text=[{"qualified_name":"public.orders",...

[tool call] describe_table({"qualified_name": "public.orders"})
[tool result] is_error=False, text={"qualified_name":"public.orders",...

[assistant turn 3] Customer order totals are stored in `public.orders.total_cents` (INTEGER, in cents). The orders table joins to `public.users` via `user_id`...

[done] stopped at turn 3, stop_reason='end_turn'
```

The script is **bounded by `--max-turns` (default 8)** and aborts cleanly if the agent doesn't converge. Cost on Haiku 4.5 is typically $0.005-0.02 per run.

This is the same path Claude Desktop takes internally (stdio MCP + tool-use loop), just without the chat UI. Use it to:

- Verify your install before debugging Claude Desktop config.
- Smoke-test SchemaBrain in CI.
- Crib the agent loop into your own application.

## Logs

SchemaBrain has a single, deliberately simple logging system: **one stream,
stderr.** No log files, no rotation, no JSON output. The default level is
`WARNING`, so healthy runs are essentially silent. Raise the level when you
need to debug; lower it when the noise gets in the way.

How you raise the level depends on **how** SchemaBrain is running.

### When you're running `schemabrain` in a terminal

Pass `-v` flags. Counted — more `v`s, more output:

```bash
schemabrain      index <url>     # WARNING (default, near-silent)
schemabrain -v   index <url>     # INFO and above
schemabrain -vv  index <url>     # DEBUG and above
```

The lines appear on your terminal's stderr, interleaved with the progress
bar and the summary line. The Rich progress bar and the log lines coexist
on the same stream without garbling.

To capture for later:

```bash
schemabrain -v index <url> 2> schemabrain.log
```

### When Claude Desktop is launching `schemabrain serve` for you

You **don't have a terminal**. Claude Desktop spawns the server in the
background, so `-v` is not an option — there's no command line you control.

Instead, you set an environment variable in **Claude Desktop's** config
file (not SchemaBrain's — SchemaBrain has no config file). On macOS that
file lives at:

```
~/Library/Application Support/Claude/claude_desktop_config.json
```

A working entry looks like this:

```json
{
  "mcpServers": {
    "schemabrain": {
      "command": "/usr/local/bin/schemabrain",
      "args": [
        "serve",
        "--url-env", "DATABASE_URL",
        "--store-path", "/Users/you/.schemabrain.db"
      ],
      "env": {
        "DATABASE_URL": "postgresql+psycopg://postgres:local@localhost:5432/postgres",
        "ANTHROPIC_API_KEY": "sk-ant-…",
        "SCHEMABRAIN_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

What each block does:

| Field | What it does |
|---|---|
| `command` | The `schemabrain` binary to launch. Find your path with `which schemabrain`. |
| `args` | Command-line arguments — same as if you typed them in a terminal. |
| `env` | Environment variables for the spawned process. `SCHEMABRAIN_LOG_LEVEL` is the only logging-relevant one. |

`SCHEMABRAIN_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL` (case-insensitive). Unrecognized values fall back to `WARNING`
and emit a one-line warning to stderr so you know there was a typo.

**Where the lines actually appear:** Claude Desktop captures the stderr of
every MCP server it spawns. On macOS, read it with:

```bash
tail -f ~/Library/Logs/Claude/mcp-server-schemabrain.log
```

That's the file to watch when something goes wrong inside `serve` — you'll
see the traceback there even though no terminal was ever open.

### Precedence

If you happen to set both a `-v` flag and `SCHEMABRAIN_LOG_LEVEL`, the
**flag wins.** The env var is a fallback for the case where you can't pass
flags (i.e. Claude Desktop). In a terminal, prefer the flag.

### What's deliberately not there

| Not implemented | Why |
|---|---|
| Log files | One stream is simpler. Use shell redirection if you need persistence. |
| JSON / structured logging | Only one call site exists today (the MCP boundary catch). Will revisit if more land. |
| Per-module level overrides | SchemaBrain has one namespace. Third-party loggers (`mcp`, `anyio`, `httpx`, `httpcore`, `fastembed`) are pinned at WARNING regardless of our level so `-vv` doesn't drown you in SDK chatter. |
| Log rotation | Not our concern. Claude Desktop manages rotation of `mcp-server-schemabrain.log` on its side; for terminal runs, your shell redirect manages the file. |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: ANTHROPIC_API_KEY not set` | Key not exported | `export ANTHROPIC_API_KEY=sk-ant-...` |
| `ModuleNotFoundError: No module named 'psycopg2'` | Used `postgresql://` scheme | Switch to `postgresql+psycopg://` |
| Claude Desktop doesn't see the tools | Config path or syntax error | Tail `~/Library/Logs/Claude/mcp*.log` |
| `error: could not open store at './schemabrain.db'` | Relative path in Claude Desktop config | Use absolute paths |
| Agent loop hits `max-turns` cap | Question too broad or store under-indexed | Re-run `index` first; ask a more specific question |
| Tool returns `isError=True` with "is not in the store" | Store source ID doesn't match `--source` URL | `--source` must match the URL passed to `index` exactly |

### Inspecting tool shapes with the official MCP Inspector

The Model Context Protocol team publishes an interactive inspector
that connects to any MCP server over stdio. It's the cleanest way to
see the JSON schemas SchemaBrain exposes — including the per-arg
descriptions — without needing Claude Desktop, Cursor, or the
Anthropic SDK.

```bash
# No install — npx runs the latest published version. Requires Node.js 18+.
npx @modelcontextprotocol/inspector \
    schemabrain serve \
        --url-env DATABASE_URL \
        --store-path ./schemabrain.db
```

The inspector opens a browser tab showing every registered tool, its
description, the input JSON schema (with per-argument descriptions),
and a live call-and-response panel. Use it to:

- Verify the server boots cleanly against your store + connection.
- See exactly what shape an agent will see when it calls
  `find_relevant_tables`, `describe_table`, `describe_column`,
  `get_example_queries`, or `suggest_joins`.
- Trigger each tool with hand-crafted args and read the structured
  `ToolResponse` envelope directly.

`DATABASE_URL` must be exported in the same shell that runs the
`npx` command, since the inspector spawns `schemabrain serve` as a
subprocess and inherits your environment.

## Validating SQL Claude generates

SchemaBrain gives Claude rich context, but it doesn't run the SQL. The agent
produces queries that *should* be correct — but you should still verify before
trusting the output, especially on real data. A four-step ladder, cheapest to
most thorough:

### Step 1 — does it execute?

Pipe the SQL through `psql` (or `docker exec -i sb-pg psql ...` if you don't
have host psql). Any syntax error, wrong column, or wrong qualified name fails
here in milliseconds:

```bash
docker exec -i sb-pg psql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
<paste Claude's SQL here>
SQL
```

If it returns rows (or 0 rows cleanly), the schema-level correctness is proven.

### Step 2 — is the query plan sane?

Catches accidental cartesian products, missing indexes, and surprise sequential
scans on large tables:

```bash
docker exec -i sb-pg psql -U postgres -d postgres <<'SQL'
EXPLAIN (ANALYZE, BUFFERS) <Claude's SQL here>;
SQL
```

Look for: hash joins or nested loops on small tables (fine), sequential scans
on tables you expected to use an index (suspicious), and Rows Removed by Filter
counts that look unreasonable.

### Step 3 — does it produce the expected numbers on a known dataset?

This is the killer check. Construct a tiny dataset where you can hand-compute
the right answer, then run Claude's query and compare. **Especially valuable
when the agent flagged a caveat** — a real test makes the caveat empirical.

Worked example with the bundled e-commerce fixture: Claude warned that products
in N categories would have their spend counted N times. Seed three orders, then
compare the per-customer total spend computed two ways — through Claude's
per-category query vs an independent reference that ignores categories:

```bash
docker exec -i sb-pg psql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
INSERT INTO public.product_categories (product_id, category_id) VALUES
    (1, 1), (1, 2), (2, 3) ON CONFLICT DO NOTHING;
INSERT INTO public.orders (id, user_id, status, total_cents, placed_at) VALUES
    (1, 1, 'paid',    23998, '2026-05-01'),
    (2, 2, 'paid',    29998, '2026-05-02'),
    (3, 3, 'pending',  8999, '2026-05-03') ON CONFLICT DO NOTHING;
INSERT INTO public.order_items (id, order_id, product_id, quantity, unit_price_cents) VALUES
    (1, 1, 1, 1,  8999), (2, 1, 2, 1, 14999),
    (3, 2, 2, 2, 14999), (4, 3, 1, 1,  8999) ON CONFLICT DO NOTHING;
SQL
```

Independent reference (no categories) — the actual money each customer paid:

```bash
docker exec -i sb-pg psql -U postgres -d postgres <<'SQL'
SELECT u.id, u.full_name,
       SUM(oi.quantity * oi.unit_price_cents) / 100.0 AS actual_spend
FROM   public.users u
JOIN   public.orders o       ON o.user_id   = u.id
JOIN   public.order_items oi ON oi.order_id = o.id
GROUP BY u.id, u.full_name ORDER BY u.id;
SQL
```

You'll see Alice $239.98, Bob $299.98, Cara $89.99. Now run Claude's
per-category query (the one from the README) and naively sum its rows per
customer — Alice will inflate to $329.97 and Cara to $179.98 because their
shoes are tagged in two categories. **The caveat Claude flagged is the
mechanical truth of the data.** This is why agents that flag M:N caveats are
worth more than agents that don't.

### Step 4 — sanity-check against production sample

For NULL handling, deprecated rows, and weird data shapes that only appear in
production. `LIMIT 100` against prod, eyeball the output. No automated rule
catches every category of issue this stage does.

## What's next

- Run `schemabrain eval` to score retrieval quality against the bundled e-commerce golden set (or your own).
- Re-run `schemabrain index` whenever your schema changes — it's idempotent and cache-aware.
- For schemas with cryptic column names (`acct_dim_v3`, `pmt_fct_h`), pass `--enable-sonnet` at index time to route those to Claude Sonnet 4.6 for better decoding (~5x cost per affected column).
