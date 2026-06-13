# SchemaBrain "end to end" demo (curated edition) — charmbracelet/vhs.
#
#     vhs docs/assets/demo-cli-curated.tape  →  docs/assets/demo-cli-curated.gif
#
# Same three-movement story as demo-cli.tape, but the semantic layer is
# the curated bundled SaaS pack instead of a live LLM run. That makes the
# whole demo deterministic, free (no API key), fast, and fully coherent
# (the setup counts MATCH the firewall + operator beats — no live-LLM
# count seam). Snappier pacing so the viewer reaches the firewall fast.
#
#   I.   index + apply — a live schema becomes a governed knowledge graph
#   II.  demo --showcase — the firewall computes, refuses, recovers
#   III. operator console — inspect / policy / audit / check / doctor
#
# Prerequisites (see CONTRIBUTING "Recording the demos"):
#   - Repo root, venv built (`uv sync`). Hidden preamble runs
#     `source .venv/bin/activate` so the spawned shell resolves the CLI.
#   - Docker up with the demo Postgres `sb-demo-pg` (127.0.0.1:5433,
#     password `local`). index / apply / check connect to it.
#   - No ANTHROPIC_API_KEY needed. The hidden preamble exports the curated
#     SaaS pack to YAML (code is the single source of truth) for `apply`.

Output docs/assets/demo-cli-curated.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 1000
Set Theme "Dracula"
Set TypingSpeed 28ms
Set PlaybackSpeed 1.0

# --- Hidden preamble (not recorded) -------------------------------------
# Resolve the CLI, set a clean prompt, and export the curated SaaS pack
# from the code-built store so Movement I can `apply` it.
Hide
Type "source .venv/bin/activate"
Enter
Type "export PS1='$ '"
Enter
Type "export DEMO_URL='postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres'"
Enter
Type "rm -rf /tmp/sbpack /tmp/sb-seed.db* ./demo-cli.db*"
Enter
Type "schemabrain demo --showcase --store-path /tmp/sb-seed.db >/dev/null 2>&1"
Enter
Sleep 3s
Type "schemabrain entities export-all --store-path /tmp/sb-seed.db --dir /tmp/sbpack/entities >/dev/null 2>&1"
Enter
Sleep 800ms
Type "schemabrain metrics export-all --store-path /tmp/sb-seed.db --dir /tmp/sbpack/metrics >/dev/null 2>&1"
Enter
Sleep 800ms
Type "schemabrain joins export-all --store-path /tmp/sb-seed.db --dir /tmp/sbpack/joins >/dev/null 2>&1"
Enter
Sleep 800ms
Type "clear"
Enter
Show

# --- Movement I: live schema → governed knowledge graph -----------------
Type "# Hand an AI agent raw SQL and it'll hallucinate joins and leak PII. Here's the fix."
Sleep 2s
Enter
Type "schemabrain index --no-enrich --url-env DEMO_URL --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 3s
Type "# Index the live schema, then layer on the curated model — entities, joins, metrics."
Sleep 1500ms
Enter
Type "schemabrain entities apply /tmp/sbpack/entities --url-env DEMO_URL --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 2500ms
Type "schemabrain joins apply /tmp/sbpack/joins --url-env DEMO_URL --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 2500ms
Type "schemabrain metrics apply /tmp/sbpack/metrics --url-env DEMO_URL --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 2500ms

# --- Movement II: the firewall (the hook) -------------------------------
Type "# Now the agent asks in plain English. Safe metric → a number. Leak → refused. No SQL."
Sleep 2s
Enter
Type "schemabrain demo --showcase --store-path ./demo-cli.db"
Sleep 600ms
Enter
Sleep 12s

# --- Movement III: see and audit everything -----------------------------
Type "# Inspect everything the agent can see — and audit every call it made."
Sleep 2s
Enter
Type "schemabrain inspect --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 4s
Type "schemabrain inspect user --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "schemabrain policy show --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "schemabrain audit list --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "schemabrain audit verify --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 4s
Type "schemabrain check --url-env DEMO_URL --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 4s
Type "schemabrain doctor --host manual --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 4s
Type "# The agent reads. It never writes SQL. That's the whole point."
Sleep 4s
