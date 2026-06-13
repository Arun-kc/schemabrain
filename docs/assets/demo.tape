# SchemaBrain demo recording — scripted with charmbracelet/vhs.
#
# vhs renders this tape into a reproducible animated GIF used as the
# README hero. Re-render after any user-visible change to
# `schemabrain demo --showcase` or `schemabrain audit verify`:
#
#     vhs docs/assets/demo.tape
#
# The recorded flow is the ZERO-SETUP showcase — no API key, no Docker,
# no Postgres. Three acts (~40s):
#   1. schemabrain demo --showcase — the firewall in five get_metric
#      calls: it COMPUTES the safe metric, REFUSES the two leaks (PII +
#      credential), and RECOVERS the two unreachable/ambiguous calls,
#      then verifies the seeded tamper-evident audit chain.
#   2. schemabrain audit verify   — re-proves the chain independently.
#   3. punchline                  — the agent never wrote SQL.
#
# Prerequisites for re-recording:
#   - Run from the repo root with the project venv built
#     (`uv sync`). The tape's hidden preamble runs
#     `source .venv/bin/activate` so the freshly-spawned recording
#     shell can resolve `schemabrain` — without it the shell vhs spawns
#     does NOT have the CLI on PATH and every command renders as
#     "command not found". (This is the failure the rewrite fixes.)
#   - No API key, no Docker, no Postgres: the showcase replays through
#     the real MCP server against the bundled offline SaaS store and a
#     stubbed executor, so every refusal is real and the whole run is
#     ~1s. The long Sleeps below are pure read time, not compute.
#   - The store is written to ./demo.db (gitignored / disposable) so
#     the printed dashboard hint stays a clean relative path rather than
#     leaking an absolute home-directory path.
#   - vhs installed (`brew install vhs` or
#     `go install github.com/charmbracelet/vhs@latest`).

Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 860
Set Theme "Dracula"
Set TypingSpeed 30ms
Set PlaybackSpeed 1.0

# Hidden preamble: put `schemabrain` on PATH, set a minimal prompt, and
# start from a clean screen. None of this is recorded — the GIF opens on
# the first visible keystroke of Act 1.
Hide
Type "source .venv/bin/activate"
Enter
Type "export PS1='$ '"
Enter
Type "clear"
Enter
Show

# --- Act 1: the showcase --------------------------------------------------
Type "# One command. No API key, no Docker, no Postgres."
Sleep 1s
Enter
Type "# Watch the firewall compute the safe metric — and refuse the leaks."
Sleep 1s
Enter
Type "schemabrain demo --showcase --store-path ./demo.db"
Sleep 600ms
Enter
# Builds the bundled SaaS store (12 entities · 11 joins · 5 metrics),
# then replays five get_metric calls through the REAL MCP server:
#   ✓ revenue by plan tier        — computed, no SQL written
#   ✕ users by email address      — pii_blocked (row-level disclosure)
#   ⚠ usage volume by plan tier   — unreachable_entity + resolve_join
#   ⚠ revenue over time           — ambiguous_time_dimension
#   ✕ accounts by password hash   — pii_blocked (credential floor)
#   ✓ audit chain intact (5 calls, tamper-evident)
# Runs in ~1s; the Sleep is read time for the whole scorecard.
Sleep 14s

# --- Act 2: independent audit verification --------------------------------
Type "# Every one of those calls was logged to a tamper-evident chain."
Sleep 1s
Enter
Type "schemabrain audit verify --store-path ./demo.db"
Sleep 600ms
Enter
Sleep 6s

# --- Act 3: the punchline -------------------------------------------------
Type "# Computed the metric. Refused the leaks. Logged all of it."
Sleep 1500ms
Enter
Type "# The agent never wrote SQL. SchemaBrain did."
Sleep 3s
