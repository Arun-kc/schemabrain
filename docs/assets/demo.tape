# Schema Brain demo recording — scripted with charmbracelet/vhs.
#
# vhs renders this tape into a reproducible animated GIF. The output is
# read by the README hero. Re-render after any user-visible CLI surface
# change:
#
#     vhs docs/assets/demo.tape
#
# The four acts (run-time ~45s):
#   1. schemabrain init   — five-stage activation wizard
#   2. schemabrain inspect — operator-side browser, no LLM, no agent
#   3. MCP get_metric call — Claude resolves a metric through the server
#   4. schemabrain tail   — live stream of the tool call from act 3
#
# Prerequisites for re-recording:
#   - A running Postgres at localhost:5432 with the bundled ecommerce
#     fixture applied (see README quickstart for the one-liner).
#   - ANTHROPIC_API_KEY exported in the recording shell so stage 3 of
#     init does not skip.
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
Type "schemabrain init --url-env DATABASE_URL --store-path ./demo.db"
Enter
# The wizard renders five stages with per-stage timing — leave room
# for it to print to completion before continuing.
Sleep 8s

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
Sleep 12s

# --- Act 4: tail ----------------------------------------------------------
Type "# Every tool call lands here in real time."
Sleep 800ms
Enter
Type "schemabrain tail --since 5m"
Enter
Sleep 4s

Type "# The agent never wrote SQL. Schema Brain did."
Sleep 2s
