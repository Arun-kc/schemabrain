# SchemaBrain "end to end" demo — scripted with charmbracelet/vhs.
#
#     vhs docs/assets/demo-cli.tape   →   docs/assets/demo-cli.gif
#
# Headless: no agent, no MCP host. Three movements:
#   I.   init — a live Postgres schema becomes a knowledge graph. This is
#        the REAL wizard with live LLM curation, so the entity/metric
#        stages visibly progress ("Asking Claude to identify entities…"
#        → "…define metrics…") instead of popping in.
#   II.  demo --showcase — the firewall refuses leaks + emits recovery
#        contracts.
#   III. operator console — inspect / policy / audit / check / doctor.
#
# Prerequisites (see CONTRIBUTING "Recording the demos"):
#   - Run from repo root with the venv built (`uv sync`). The hidden
#     preamble runs `source .venv/bin/activate` so the fresh shell vhs
#     spawns can resolve `schemabrain` (without it: "command not found").
#   - Docker up with the demo Postgres `sb-demo-pg` (127.0.0.1:5433,
#     password `local`). init / showcase-rebuild / check connect to it.
#   - ANTHROPIC_API_KEY available. The CLI auto-loads it from the repo
#     `.env`; init's stages 03/04 make ~2 Sonnet calls (~$0.04/render).
#   - Pre-warm the fastembed cache once (`schemabrain init …`) so the
#     index stage doesn't download the model mid-record.
#
# Note: live `init` curates a variable LLM entity/metric count; the
# firewall showcase + operator tour use the fixed bundled SaaS pack
# (12 entities / 5 metrics), so the init counts won't match the later
# inspect/check counts. This is intentional (the audit beats require the
# showcase to rebuild the store with the bundled pack).

Output docs/assets/demo-cli.gif

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 1000
Set Theme "Dracula"
Set TypingSpeed 34ms
Set PlaybackSpeed 1.0

# --- Hidden preamble (not recorded) -------------------------------------
Hide
Type "source .venv/bin/activate"
Enter
Type "export PS1='$ '"
Enter
Type "export DEMO_URL='postgresql+psycopg://postgres:local@127.0.0.1:5433/postgres'"
Enter
Type "rm -rf ./demo-cli.db*"
Enter
Type "clear"
Enter
Show

# --- Movement I: live schema → knowledge graph (real wizard) ------------
Type "# Point SchemaBrain at a live Postgres. Watch it build the knowledge graph."
Sleep 1500ms
Enter
Type "schemabrain init --url-env DEMO_URL --store-path ./demo-cli.db --print-only --yes"
Sleep 800ms
Enter
# The wizard runs source_check → index → curate entities (Sonnet) →
# curate metrics (Sonnet) → curate joins → wire_host. Stages 03/04 show
# live "Asking Claude…" spinners (~15s + ~17s). 44s covers the run plus a
# read of the final table; bump if a slow LLM run gets cut mid-stage.
Sleep 44s

# --- Movement II: the firewall + structured recovery --------------------
Type "# The firewall: it computes the safe metric and refuses the leaks."
Sleep 1500ms
Enter
Type "schemabrain demo --showcase --store-path ./demo-cli.db"
Sleep 600ms
Enter
Sleep 13s

# --- Movement III: the operator's console -------------------------------
Type "# Everything the agent sees — read-only — and everything it did."
Sleep 1500ms
Enter
Type "schemabrain inspect --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "schemabrain inspect user --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 6s
Type "schemabrain policy show --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 6s
Type "schemabrain audit list --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 6s
Type "schemabrain audit verify --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "schemabrain check --url-env DEMO_URL --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "schemabrain doctor --host manual --store-path ./demo-cli.db"
Sleep 500ms
Enter
Sleep 5s
Type "# Indexed, curated, firewalled, audited — no SQL, no agent, no leaks."
Sleep 4s
