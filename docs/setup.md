# Schema Brain — Setup

Two paths from "I have a Postgres database" to "an AI agent can answer questions about it":

1. **MCP client (Claude Desktop or Cursor)** — for everyday use; click into the chat and ask questions.
2. **Anthropic SDK demo** — for verifying the install end-to-end without Claude Desktop, and for adapting Schema Brain into your own agent code.

Both share the same indexing step.

## 0. Install + index

```bash
# Install (in a venv)
pip install schemabrain

# Or from source if you want to hack on it:
#   git clone https://github.com/Arun-kc/schemabrain && cd schemabrain
#   uv sync --extra dev
# (source-install users prefix the runtime commands below with `uv run`)

# Set your Anthropic key (used at index time for column descriptions)
export ANTHROPIC_API_KEY=sk-ant-...

# Index your database. URL MUST use the postgresql+psycopg:// scheme
# (Schema Brain uses psycopg v3; the bare postgresql:// scheme fails
# with ModuleNotFoundError).
schemabrain index \
    "postgresql+psycopg://user:pass@host:5432/dbname" \
    --store-path ./schemabrain.db
```

The index step:
- Reflects every user-visible table.
- Generates one LLM-written column description per column (Claude Haiku 4.5; ~$0.0003/column at typical schema density).
- Embeds each description locally with `BAAI/bge-small-en-v1.5` (~67 MB ONNX, ~10 ms/column warm).
- Persists everything to `./schemabrain.db`.

Re-running `index` against an unchanged schema is a **no-op** — 0 LLM calls, 0 embedder calls, ~0.1 s. Schema-changed tables are re-enriched + re-embedded selectively.

For cost-free dry runs (no LLM, no embeddings), pass `--no-enrich`.

## Path 1 — Claude Desktop (or Cursor)

Add Schema Brain to Claude Desktop's MCP server config:

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
        "--source",
        "postgresql+psycopg://USER:PASSWORD@HOST:5432/DBNAME",
        "--store-path",
        "/ABSOLUTE/PATH/TO/schemabrain.db"
      ]
    }
  }
}
```

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

python anthropic_demo.py \
    --source "postgresql+psycopg://user:pass@host:5432/dbname" \
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
- Smoke-test Schema Brain in CI.
- Crib the agent loop into your own application.

## Logs

Schema Brain has a single, deliberately simple logging system: **one stream,
stderr.** No log files, no rotation, no JSON output. The default level is
`WARNING`, so healthy runs are essentially silent. Raise the level when you
need to debug; lower it when the noise gets in the way.

How you raise the level depends on **how** Schema Brain is running.

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
file (not Schema Brain's — Schema Brain has no config file). On macOS that
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
        "--source", "postgresql+psycopg://postgres:local@localhost:5432/postgres",
        "--store-path", "/Users/you/.schemabrain.db"
      ],
      "env": {
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
| Per-module level overrides | Schema Brain has one namespace. Third-party loggers (`mcp`, `anyio`, `httpx`, `httpcore`, `fastembed`) are pinned at WARNING regardless of our level so `-vv` doesn't drown you in SDK chatter. |
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

## Validating SQL Claude generates

Schema Brain gives Claude rich context, but it doesn't run the SQL. The agent
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
