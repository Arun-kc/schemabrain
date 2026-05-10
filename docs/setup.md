# Schema Brain — Setup

Two paths from "I have a Postgres database" to "an AI agent can answer questions about it":

1. **Claude Desktop** — for everyday use; click into Claude and ask questions.
2. **Anthropic SDK demo** — for verifying the install end-to-end without Claude Desktop, and for adapting Schema Brain into your own agent code.

Both share the same indexing step.

## 0. Install + index

```bash
# Clone + install
git clone https://github.com/arunkc/schemabrain && cd schemabrain
uv sync --extra dev

# Set your Anthropic key (used at index time for column descriptions)
export ANTHROPIC_API_KEY=sk-ant-...

# Index your database. URL MUST use the postgresql+psycopg:// scheme
# (Schema Brain uses psycopg v3; the bare postgresql:// scheme fails
# with ModuleNotFoundError).
uv run schemabrain index \
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

## Path 1 — Claude Desktop

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

**Important: paths must be absolute.** Claude Desktop runs your config in a different working directory than your shell, so relative paths and `~` won't resolve. Run `realpath ./schemabrain.db` and `which schemabrain` (after `uv sync`, with the project venv activated) to get the absolute paths.

Restart Claude Desktop. In a new conversation you should see the `schemabrain` MCP server listed in the tool tray. Try a question like "Which tables in my database describe orders?" — Claude will call `find_relevant_tables` and `describe_table` to answer.

## Path 2 — Anthropic SDK demo (no Claude Desktop required)

The demo script in `examples/anthropic_demo.py` spawns `schemabrain serve` over stdio, drives Claude Haiku via the Anthropic SDK's standard tool-use loop, and prints the conversation transcript:

```bash
export ANTHROPIC_API_KEY=sk-ant-...

uv run python examples/anthropic_demo.py \
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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `error: ANTHROPIC_API_KEY not set` | Key not exported | `export ANTHROPIC_API_KEY=sk-ant-...` |
| `ModuleNotFoundError: No module named 'psycopg2'` | Used `postgresql://` scheme | Switch to `postgresql+psycopg://` |
| Claude Desktop doesn't see the tools | Config path or syntax error | Tail `~/Library/Logs/Claude/mcp*.log` |
| `error: could not open store at './schemabrain.db'` | Relative path in Claude Desktop config | Use absolute paths |
| Agent loop hits `max-turns` cap | Question too broad or store under-indexed | Re-run `index` first; ask a more specific question |
| Tool returns `isError=True` with "is not in the store" | Store source ID doesn't match `--source` URL | `--source` must match the URL passed to `index` exactly |

## What's next

- Run `schemabrain eval` to score retrieval quality against the bundled e-commerce golden set (or your own).
- Re-run `schemabrain index` whenever your schema changes — it's idempotent and cache-aware.
- For schemas with cryptic column names (`acct_dim_v3`, `pmt_fct_h`), pass `--enable-sonnet` at index time to route those to Claude Sonnet 4.6 for better decoding (~5x cost per affected column).
