# Manual test recipe — PR-2 (feat/post-pr79-polish-bundle)

**Branch:** `feat/post-pr79-polish-bundle` — 10 commits (8 features + plan + Round-1 fold). **DO NOT** push or open a PR until every step below passes.

This is the mandatory end-to-end smoke per `feedback_manual_smoke_mandatory.md`. Unit tests passing (4234 in this PR) does NOT substitute for running the CLI as a real user would.

PR-2 ships 8 new surfaces; each step below exercises one or more:

- **D2** — auto-`docker run` from stage 0 (the marquee win)
- **D4** — `.env` persist for `ANTHROPIC_API_KEY` with opt-in consent
- **D3** — unified-diff preview before host-overwrite prompt
- **F3** — inline overwrite prompt in stage 6 (no orphan from hero)
- **F4** — "reusing N tables from a prior indexing run" framing
- **F5** — graceful 529/401 LLM-failure rendering (no traceback)
- **F1 + D1** — cost preamble + Rich Live elapsed-timer spinner for standalone suggest commands

---

## 0. Pre-flight: clean slate

These tests are sensitive to leftover state. Reset everything first.

```bash
# Stop and remove the demo Postgres container (D2 will recreate it)
docker rm -f sb-demo-pg 2>/dev/null

# Backup + clear Claude Desktop schemabrain entry so the overwrite flow
# fires cleanly. On macOS:
cp ~/Library/Application\ Support/Claude/claude_desktop_config.json \
   ~/Library/Application\ Support/Claude/claude_desktop_config.json.smoke-bak 2>/dev/null
# Then delete the "schemabrain" key under "mcpServers" (or delete the whole
# file if you have no other MCP servers configured).

# Wipe any local store files + stale .env
rm -f schemabrain.db schemabrain.db-shm schemabrain.db-wal
rm -f schemabrain-smoke.db schemabrain-smoke.db-shm schemabrain-smoke.db-wal
rm -f .env

# Confirm clean env
unset ANTHROPIC_API_KEY DATABASE_URL MY_DB_URL

# Branch state
git checkout feat/post-pr79-polish-bundle
git log --oneline main..HEAD | wc -l   # should print 10
```

## 1. Fresh install in a clean venv

```bash
rm -rf .venv-pr2-smoke
python3 -m venv .venv-pr2-smoke
source .venv-pr2-smoke/bin/activate
pip install -e .

schemabrain --version    # → 0.3.0
```

## 2. D2 — auto-docker from stage 0 (the marquee win)

**What we're verifying**: a new user with Docker installed but no Postgres running should be able to type `schemabrain init` with no source URL and have stage 0 auto-spin a container + load the bundled fixture.

```bash
schemabrain init
```

**Expected** (interactive):
- Stage 0 prompt: "Would you like to spin up a demo Postgres?" → answer `y`
- Console shows `docker run --rm -d --name sb-demo-pg ...` running
- Postgres readiness probe waits up to ~30s (status indicator)
- Fixture loads via `docker exec ... psql ...`
- Wizard proceeds into stages 1-7 against `postgresql+psycopg://postgres:local@localhost:5433/postgres`
- **No psycopg2 errors** (silent driver rewrite working)
- After stages 3+4, you should see the **D4 consent prompt** (covered in step 5) — for now press Enter to decline

**Verify the container is there:**

```bash
docker ps --filter name=sb-demo-pg --format '{{.Names}} {{.Status}}'
# → sb-demo-pg  Up X seconds
```

**Idempotency check** — re-run `schemabrain init` immediately:

- Stage 0 should detect the existing `sb-demo-pg` container, skip the `docker run`, just confirm readiness
- Stage 2 should print **F4 framing**: `reusing 7 table(s) from a prior indexing run (./schemabrain.db)`

## 3. F1 + D1 — cost preamble + Live elapsed-timer spinner

**What we're verifying**: when you run a standalone `entities suggest` or `metrics suggest`, you see the cost preview AND a Live spinner with elapsed seconds (not a frozen-looking process).

> **Note**: D2's demo Postgres binds to `localhost:5433` (deliberately avoiding the dev-local 5432) and only writes the URL into the host config — it does NOT export `DATABASE_URL` in your shell. Standalone suggest commands need the var, so export it first:

```bash
export DATABASE_URL="postgresql+psycopg://postgres:local@localhost:5433/postgres"
unset ANTHROPIC_API_KEY
rm -f .env
schemabrain entities suggest \
  --url-env DATABASE_URL \
  --store-path ./schemabrain.db \
  --dry-run
```

**Expected**:

- Cost preamble: `◆ Schema Brain uses Claude to suggest entities.` + cost/time/skip hint
- Password-masked prompt: `Paste ANTHROPIC_API_KEY:`
- Paste your real `sk-ant-...` key
- **D4 consent prompt** fires (see step 5 for the contract)
- During the LLM call: a Live spinner shows `▸ suggesting entities · sonnet · ~$0.01 (capped at $0.50) · 5s` — **the seconds counter updates in real time**

## 4. F3 + D3 — inline overwrite prompt with unified-diff preview

**What we're verifying**: re-running `init` against an existing config no longer produces an orphan "overwrite?" prompt between the hero and stage list — it fires INLINE during stage 6, AND shows a unified diff of what's about to change.

```bash
# Re-run init with a DIFFERENT --store-path AND --env-var-name to trigger
# the "differs" verdict (not "differs_store_path_only" which auto-accepts).
export MY_DB_URL="postgresql+psycopg://postgres:local@localhost:5433/postgres"
schemabrain init \
  --url-env MY_DB_URL \
  --store-path ./schemabrain-smoke.db \
  --env-var MY_DB_URL
```

**Expected** at stage 6:

- Yellow header: `A different schemabrain entry already exists in /Users/.../claude_desktop_config.json.`
- `differing fields: env, args`
- **Unified diff body** with red `-` and green `+` lines:

  ```
  --- /Users/.../claude_desktop_config.json (current)
  +++ /Users/.../claude_desktop_config.json (after init)
  @@ -1,5 +1,5 @@
  -    "args": [..., "--store-path", "./schemabrain.db", ...]
  +    "args": [..., "--store-path", "./schemabrain-smoke.db", ...]
  -    "env": { "DATABASE_URL": ... }
  +    "env": { "MY_DB_URL": ... }
  ```

- Inline prompt: `Overwrite the existing schemabrain entry? [y/N]`
- **Press `n`** → wizard exits cleanly with **exit code 0** (user-cancel, not error):

  ```bash
  echo $?    # → 0
  ```

- Re-run the same command, this time **press `y`** → overwrite proceeds:

  ```bash
  ls -la ~/Library/Application\ Support/Claude/claude_desktop_config.json*
  # Should show .bak sibling (created on first overwrite, per PR-1 contract)
  ```

## 5. D4 — `.env` persist with opt-in consent + gitignore warning

**What we're verifying**: after a successful interactive key paste, the wizard offers to persist the key to `.env`, **defaults to NO**, and warns when `.env` isn't gitignored.

### 5a. The decline path

```bash
# DATABASE_URL must be exported (see step 3's note about demo Postgres
# only writing the URL into the host config, not the shell):
export DATABASE_URL="postgresql+psycopg://postgres:local@localhost:5433/postgres"
unset ANTHROPIC_API_KEY
rm -f .env .gitignore

schemabrain entities suggest \
  --url-env DATABASE_URL \
  --store-path ./schemabrain.db \
  --dry-run
# Paste your sk-ant-... key
# At the consent prompt, press Enter (default no)
```

**Expected**:

- **Yellow warning line** (no `.gitignore` present): `⚠ .env is NOT listed in .gitignore — saving here will commit the key if you 'git add' this file.`
- Prompt: `Save ANTHROPIC_API_KEY to .env for next time? [y/N]`
- Pressing Enter (default no) → command proceeds, **no .env created**:

  ```bash
  ls -la .env 2>&1    # → No such file or directory
  ```

### 5b. The accept path

```bash
# Add .env to a fresh .gitignore so the warning disappears
echo ".env" > .gitignore

unset ANTHROPIC_API_KEY
schemabrain entities suggest \
  --url-env DATABASE_URL \
  --store-path ./schemabrain.db \
  --dry-run
# Paste your sk-ant-... key
# At the consent prompt, type "y"
```

**Expected**:

- **No yellow warning** (`.env` is in `.gitignore` now)
- Prompt fires; type `y`
- Confirmation: `✓ saved to /path/to/.env — next run loads it automatically; never overrides an explicit export.`
- File created with 0o600 perms:

  ```bash
  ls -la .env    # → -rw-------
  cat .env       # → ANTHROPIC_API_KEY=sk-ant-...
  ```

### 5c. Verify the load + shell-export-wins contract

```bash
unset ANTHROPIC_API_KEY    # clear shell
schemabrain entities list --store-path ./schemabrain.db
# Should work — .env auto-loaded by main()

# Now confirm shell export beats .env
export ANTHROPIC_API_KEY=sk-ant-FRESH
python3 -c "
from schemabrain.setup.env_file import load_env_file_into_environ
from pathlib import Path
load_env_file_into_environ(Path('.env'))
import os
print(os.environ['ANTHROPIC_API_KEY'])
"
# → sk-ant-FRESH (not the stale .env value)
```

## 6. F5 — graceful LLM-failure rendering (no traceback)

**What we're verifying**: a 401 / 429 / 529 renders a friendly guided block, not a Python traceback.

Cleanest forcing: bogus API key for 401.

```bash
ANTHROPIC_API_KEY=sk-ant-INVALID \
  schemabrain entities suggest \
    --url-env DATABASE_URL \
    --store-path ./schemabrain.db \
    --dry-run
```

**Expected**:

- **No raw Python traceback**
- Guided block with title like `error: Claude rejected the request` and a `next:` line suggesting how to recover (rotate key / check billing / retry)
- Exit code 2

## 7. Cleanup

```bash
# Stop the demo Postgres container
docker rm -f sb-demo-pg

# Restore your Claude Desktop config from the backup if you made one
mv ~/Library/Application\ Support/Claude/claude_desktop_config.json.smoke-bak \
   ~/Library/Application\ Support/Claude/claude_desktop_config.json 2>/dev/null

# Wipe smoke artifacts
rm -f schemabrain.db schemabrain-smoke.db .env .gitignore
deactivate
rm -rf .venv-pr2-smoke
```

---

## Pass criteria

All 7 steps produce the expected output. Any deviation is a bug to:

- **fold into PR-2** if found pre-push (use the same 3-agent rotation discipline as PR-#79 Round-3/Round-4)
- **file in PR-2 body as known limitation** if found post-merge

## What this recipe deliberately does NOT cover

- Real-source indexing of a non-demo Postgres (covered by `tests/test_setup_init_postgres_e2e.py`)
- MCP Inspector / Claude Desktop end-to-end conversation (visual-only, no programmable contract)
- `serve` mode tool surfaces (covered by `docs/internal/manual_smoke_2026_05_19.md` for PR-#79)
- Reviewer rotation discipline (separate workflow, see `docs/internal/pr2_round1_review_fold.md`)
