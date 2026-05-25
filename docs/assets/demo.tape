# SchemaBrain demo recording — scripted with charmbracelet/vhs.
#
# vhs renders this tape into a reproducible animated GIF. The output is
# read by the README hero. Re-render after any user-visible CLI surface
# change:
#
#     vhs docs/assets/demo.tape
#
# The four acts (run-time ~45s):
#   1. schemabrain init   — seven-stage activation wizard
#   2. schemabrain inspect — operator-side browser, no LLM, no agent
#   3. MCP get_metric call — Claude resolves a metric through the server
#   4. schemabrain tail   — live stream of the tool call from act 3
#
# Prerequisites for re-recording — see CONTRIBUTING.md, section
# "Recording the demo asset", for the full setup script:
#   - A running Postgres at localhost:5432 with the bundled ecommerce
#     fixture applied.
#   - ANTHROPIC_API_KEY exported in the recording shell so stages 3
#     (entities, LLM) and 4 (metrics, LLM) of init do NOT skip — a
#     skipped stage 3 means no entities are applied, so Act 2's
#     `inspect customer` drill exits 1 with a guided error and Act 3's
#     get_metric call has nothing to resolve. Stage 5 (joins) is
#     deterministic and runs regardless of the API key.
#   - --skip-llm-confirm passed to init so the recording doesn't hang
#     on the Enter-to-continue prompt before stages 3 + 4. The prompt
#     auto-suppresses in non-TTY environments, but a TTY-backed
#     recording shell sees the prompt; the flag bypasses it cleanly.
#   - Pre-warm the fastembed model cache by running `schemabrain init`
#     manually once before recording. The Sleep budget below assumes
#     the embedding model is already on disk; a cold first-run download
#     can take 30s+ on its own.
#   - vhs installed (`brew install vhs` or
#     `go install github.com/charmbracelet/vhs@latest`).

Output docs/assets/demo.gif

Set FontSize 14
Set Width 1200
Set Height 720
Set Theme "Dracula"
Set TypingSpeed 35ms
Set PlaybackSpeed 1.0

Hide
Type "clear"
Enter
Show

# --- Act 1: init ----------------------------------------------------------
Type "# Index a Postgres schema and wire Claude Desktop in one command."
Sleep 800ms
Enter
Type "export DATABASE_URL='postgresql+psycopg://postgres:local@localhost:5432/postgres'"
Enter
Sleep 400ms
Type "schemabrain init --url-env DATABASE_URL --store-path ./demo.db --skip-llm-confirm"
Enter
# The wizard renders seven stages: source_check → index → entities
# (LLM) → metrics (LLM) → joins (FK + query-log, no LLM) → wire_host
# → next_step. Stages 3 and 4 each make a live Anthropic call which
# adds 5–10s apiece on top of indexing. 30s is the headroom needed
# for a warm-cache run that exercises both LLM stages; bump higher
# if mid-stage cuts appear at re-render time.
Sleep 30s

# --- Act 2: inspect -------------------------------------------------------
Type "# Look at the surface the agent will see — no LLM call needed."
Sleep 800ms
Enter
Type "schemabrain inspect --store-path ./demo.db"
Enter
Sleep 3s
Type "schemabrain inspect customer --store-path ./demo.db"
Enter
Sleep 3s

# --- Act 3: MCP get_metric ------------------------------------------------
# A scripted MCP call simulating what Claude would do. The example
# uses `examples/anthropic_demo.py` to keep the demo self-contained
# and shell out to one Anthropic call rather than recording two
# separate windows.
Type "# Ask Claude (via the MCP server) for a validated metric."
Sleep 800ms
Enter
Type "python examples/anthropic_demo.py \\"
Enter
Type "    --source \"$DATABASE_URL\" \\"
Enter
Type "    --store-path ./demo.db \\"
Enter
Type "    --question 'What was revenue by month last quarter?'"
Enter
# Haiku tool-use loops typically run 8–20s depending on how many
# turns Claude takes (max 8 per the demo's cap). 18s leaves headroom
# for the worst-case run without consistently cutting mid-answer.
# If the answer still gets clipped at re-render time, bump higher.
Sleep 18s

# --- Act 4: tail ----------------------------------------------------------
Type "# Every tool call lands here in real time."
Sleep 800ms
Enter
Type "schemabrain tail --since 5m"
Enter
Sleep 4s

Type "# The agent never wrote SQL. SchemaBrain did."
Sleep 2s
