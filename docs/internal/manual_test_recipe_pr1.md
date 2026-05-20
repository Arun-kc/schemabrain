# Manual test recipe — PR-1 (feat/init-wizard-day-one-ux)

**Branch:** `feat/init-wizard-day-one-ux` — 11 commits. **DO NOT** push or open a PR until every test below passes.

This is the mandatory end-to-end smoke per `feedback_manual_smoke_mandatory.md`. Unit tests passing (4086 in this PR) does NOT substitute for running the CLI as a real user would.

---

## 0. Pre-flight: clean slate

These tests are sensitive to leftover state from previous test runs (Docker container, Postgres data, MCP host config entries, store files). Reset everything first.

```bash
# Stop and remove any previous demo Postgres container
docker rm -f sb-demo-pg 2>/dev/null

# Wipe any existing schemabrain MCP entry from Claude Desktop config (macOS).
# Open ~/Library/Application Support/Claude/claude_desktop_config.json
# and delete the "schemabrain" key under "mcpServers". Or just delete the
# whole file if you don't have other MCP servers configured.

# Remove any local store files
rm -f schemabrain.db schemabrain.db-shm schemabrain.db-wal
rm -f schemabrain-prerelease.db schemabrain-prerelease.db-shm schemabrain-prerelease.db-wal

# Confirm clean env (no schemabrain-related env vars leaked from prior runs)
env | grep -iE "schemabrain|database_url|anthropic" || echo "clean"

# Checkout the PR-1 branch and confirm 11 commits ahead of main
git checkout feat/init-wizard-day-one-ux
git log --oneline main..HEAD | wc -l   # should print 11
```

## 1. Fresh install in a clean venv

```bash
# New venv (named differently from any existing ones to avoid contamination)
rm -rf .venv-pr1-fresh
python3 -m venv .venv-pr1-fresh
source .venv-pr1-fresh/bin/activate
pip install -e .

# Sanity check the binary is wired
schemabrain --version
schemabrain init --help | head -20
```

**Expected:** version prints, init --help shows the grouped surface with `SOURCE / STAGES / HOST / COST / BEHAVIOR` sections.

## 2. Demo path — the marquee scenario

This is the one you tested earlier. Worth re-running clean to confirm the Round-3 fixes didn't regress it.

```bash
# Ensure no env shortcuts are set — we want the wizard to drive everything
unset SCHEMABRAIN_DATABASE_URL DATABASE_URL ANTHROPIC_API_KEY

# Run the wizard
schemabrain init
```

**Expected sequence:**
1. Fork prompt renders with `[2]` default (demo)
2. Press Enter to accept demo
3. Wizard shows: `✓ Docker detected on PATH`
4. Prints the `docker run` command + `psql` fixture-load command
5. **STOP** at "Press Enter to continue" — do NOT press Enter yet
6. In another terminal, run BOTH commands:
   ```bash
   docker run -d --name sb-demo-pg -p 127.0.0.1:5433:5432 -e POSTGRES_PASSWORD=local postgres:16-alpine
   docker run --rm --network host -v $(pwd)/schemabrain/eval/fixtures/ecommerce.sql:/f.sql:ro -e PGPASSWORD=local postgres:16-alpine psql -h localhost -p 5433 -U postgres -d postgres -f /f.sql
   ```
7. Back to the wizard terminal → press Enter
8. Wizard prints `✓ Using demo URL: postgresql://postgres:local@localhost:5433/postgres`
9. Stage 1 (Source check) passes — `Postgres reachable · session is read-only`
10. Stage 2 (Index schema) runs — should index 7 tables (~5-10s)
11. **Without API key**: Stages 3, 4, 5 skip with `ANTHROPIC_API_KEY not set` / `entity store is empty` messages
12. Stage 6 writes MCP config to `~/Library/Application Support/Claude/claude_desktop_config.json`
13. Stage 7 ✓
14. Closing block: discovery links to `inspect`, `doctor`, `check`, `tail`, `audit list`

**Round-2 fix verification (silent rewrite at boundary):** stage 1 must NOT crash with `ModuleNotFoundError: psycopg2`. The demo URL is bare `postgresql://` and the rewrite makes it work transparently.

## 3. Cost-preview line (LLM stages)

```bash
# Set API key so stages 3+4 actually run
export ANTHROPIC_API_KEY=sk-ant-...

# Re-run init — stages 3 and 4 will now fire
schemabrain init   # pick [2] again, press Enter at the docker wait
```

**Expected at stage 3:**
```
This stage calls Anthropic to suggest entities (cap: $1.00).
Press Enter to continue, or Ctrl-C to skip this stage.
◇ Asking Claude to identify business entities (7 tables) · ~$0.01 · capped at $1.00 · claude-sonnet-4-6
```

**Verify:**
- The `◇` cost-preview line appears BEFORE the ~30s wait
- Model name is `claude-sonnet-4-6` (NOT `claude-sonnet-4`)
- Cap matches the env var or default ($1.00 unless you set `SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD`)
- Stage 4 metrics shows similar line with `~$0.02 · capped at $0.50`

## 4. Own-DB path

```bash
unset SCHEMABRAIN_DATABASE_URL DATABASE_URL ANTHROPIC_API_KEY
rm -f schemabrain.db schemabrain.db-shm schemabrain.db-wal

schemabrain init
```

**Expected:**
1. Fork prompt fires
2. Type `1` + Enter (own-DB)
3. URL prompt: `DATABASE_URL: ` (password-masked, no echo)
4. Paste a URL — try a BARE one to verify silent rewrite: `postgresql://postgres:local@localhost:5433/postgres`
5. Wizard proceeds, stage 1 passes (silent rewrite worked — no psycopg2 error)
6. Press Ctrl-C anywhere during the wizard

**Expected on Ctrl-C:** clean `aborted.` on stderr + exit code 130. NOT a Python traceback.

```bash
echo $?   # should print 130
```

## 5. Friction commands — bare URL handling (Round-3 Bug A regression test)

This is the bug you found in your last test. Critical to confirm fixed.

```bash
unset SCHEMABRAIN_DATABASE_URL DATABASE_URL

# Test check
schemabrain check
# At prompt, paste BARE: postgresql://postgres:local@localhost:5433/postgres
# Expected: NO psycopg2 crash. Either succeeds (if store has drift checks) or
# fails with a real schemabrain error (e.g., "store not found").

# Test index
schemabrain index --store-path /tmp/pr1-test.db
# At prompt, paste BARE: postgresql://postgres:local@localhost:5433/postgres
# Expected: starts indexing, NO psycopg2 crash

# Test entities suggest
export ANTHROPIC_API_KEY=sk-ant-...
schemabrain entities suggest --store-path /tmp/pr1-test.db --apply
# At URL prompt, paste BARE URL again
# Expected: cost disclosure prompt, then LLM call, NO psycopg2 crash
```

**Critical:** none of these should crash with `ModuleNotFoundError: psycopg2`. That was the Round-3 Bug A — silent rewrite was only applied inside `_resolve_url`, but 14 callsites discarded its return. Fix: rewrite now happens in `_resolve_url_source` at the boundary.

## 6. CI safety — `--yes` must skip stage 0 (Round-3 Bug B regression test)

```bash
unset DATABASE_URL ANTHROPIC_API_KEY
# Set the env var the wizard SHOULD read via --url-env
export SCHEMABRAIN_DATABASE_URL=postgresql://postgres:local@localhost:5433/postgres

# Run with --yes + --url-env — should be FULLY non-interactive
schemabrain init --yes --url-env SCHEMABRAIN_DATABASE_URL \
  --store-path /tmp/pr1-yes-test.db \
  --no-entities --no-metrics --no-joins
```

**Expected:**
- NO fork prompt fires
- NO "Press Enter to continue" prompt
- Wizard runs through all 7 stages non-interactively
- Stage 2 indexes (or skips if already indexed)
- Stages 3+4+5 show `--no-entities set` / `--no-metrics set` / `--no-joins set` messages
- Stage 6 writes MCP config
- Exits 0
- Total runtime <10s (no LLM, no prompts)

**Critical:** this is the worst-of-both-worlds bug you found — `--yes` was hitting the fork prompt anyway and the default `[2]` was silently overriding the env-var URL with the demo URL. Fix: stage 0 gated on `not assume_yes`.

## 7. Inspect, doctor, check discovery surfaces

```bash
# Inspect — verify the new discovery block at bottom
schemabrain inspect

# Should show 7 entities (if you ran the API-key path) or empty (if not)
# Bottom should have:
#   Drill into one: schemabrain inspect <name>
#
#   Verify wiring:  schemabrain doctor
#   Detect drift:   schemabrain check
#   Watch traffic:  schemabrain tail --follow

# Doctor — verify no-store branch suggests `init` (not `index`)
rm -f /tmp/pr1-doctor-test.db
schemabrain doctor --store-path /tmp/pr1-doctor-test.db

# Expected: warn outcomes for store_schema_version and store_entity_count
# with suggested_next: "run `schemabrain init` to set up the store ..."
# (NOT "run `schemabrain index`")
```

## 8. Cleanup after testing

```bash
# Stop the demo Postgres
docker rm -f sb-demo-pg

# Remove test stores
rm -f /tmp/pr1-test.db /tmp/pr1-yes-test.db /tmp/pr1-doctor-test.db
rm -f schemabrain.db schemabrain.db-shm schemabrain.db-wal

# Remove the test venv
deactivate
rm -rf .venv-pr1-fresh

# Restore your Claude Desktop config to its prior state if needed
```

---

## What "all green" looks like

- §2 demo path completes without psycopg2 crash; MCP entry written; closing block shows discovery links
- §3 cost-preview line appears before LLM stages with `claude-sonnet-4-6` (not `-4`)
- §4 own-DB path accepts bare URL; Ctrl-C gives clean exit-130
- §5 all 3 friction commands accept bare URL without crash
- §6 `--yes` runs zero prompts
- §7 inspect shows new discovery block; doctor suggests `init` not `index`

If any of these fail, report back. If all green, the branch is ready to push.

## Next steps after all-green

```bash
git push -u origin feat/init-wizard-day-one-ux
gh pr create   # use the planning doc summary as the PR body
```
