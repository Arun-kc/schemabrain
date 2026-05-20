# PR-1 plan — `feat/init-wizard-day-one-ux`

**Branch:** `feat/init-wizard-day-one-ux` (off `main` @ `fd732fd`)
**Target:** single PR, multiple commits, NO push until user live-tests
**Companion branch:** `fix/quickstart-honesty-and-host-snippet` (5 commits, ships independently first)

## Problem

A new user reading the README Quickstart today must complete **8 manual steps** before reaching the magic moment (Claude answering "what's our total revenue?" against their schema):

1. Install Python 3.11+
2. Install Docker
3. `git clone` + `pip install -e .`
4. `docker run -d -p 5434:5432 ...` postgres:17
5. `psql ... < ecommerce.sql`
6. `export DATABASE_URL=postgresql+psycopg://...`
7. `export ANTHROPIC_API_KEY=sk-ant-...`
8. `schemabrain init`

Steps 4, 5, 6 are friction-5 (any one of them has >40% beginner bounce probability). Realistic install-to-magic time today: **15–25 minutes** with 2–3 dead-end retries.

The same env-var friction recurs across **5 other commands** (`index`, `check`, `entities suggest`, `metrics suggest`, `joins suggest`) — they all fail-with-`GuidedError` when `DATABASE_URL` or `ANTHROPIC_API_KEY` is missing.

## Goal

Collapse the new-user journey to **5 keypresses, ~4 minutes** via:

1. A new **stage 0** inside `schemabrain init` that forks demo / own-DB at the top
2. Interactive prompts for `DATABASE_URL` and `ANTHROPIC_API_KEY` when missing in TTY (with cost disclosure for the API key)
3. Snippet `env`-block injection so Claude Desktop has the URL on cold-start (no manual `export` required)
4. Replace the rapid-flash 3-label spinner pattern with a **single sticky line + elapsed timer + cost** (Option B)
5. Apply the same prompt UX to 5 post-init commands via shared `_env.py` helpers
6. Add `inspect` next-steps panel and `doctor` no-store branch for discovery

## Research base

Synthesized from 3 parallel agents (2026-05-19):

- **Codebase Onboarding Engineer** — feasibility audit; flagged wizard-handler-must-not-raise contract, snippet-bakes-env-var-name gap, indexer-is-Postgres-only
- **Product Manager** — decision matrix; recommended prompt-first fork, $0 cost cap default, deferring auto-`docker run` to PR-2
- **UX Researcher** — flow design + transcripts; recommended default fork = demo, magic-moment-is-the-conversation, anti-pattern list

3-of-3 consensus on Flow C (single command, top-of-wizard fork) + sub-step Option B + Docker Postgres demo stack + preserve `--yes` semantics.

## Decisions (user said "proceed" → defaults applied)

| # | Decision | Chosen |
|---|---|---|
| 1 | Default fork choice | `[2]` demo |
| 2 | Auto-`docker run` in PR-1 | NO — defer to PR-2 |
| 3 | Snippet shape | `env`-block injection (preserves env-var indirection + solves cold-start) |
| 4 | Sub-step fix | Option B (sticky line + elapsed timer + cost) |
| 5 | New setup stage lives in | new file `schemabrain/setup/setup_stage.py` |
| 6 | Cost cap default | $0.50 via existing `SCHEMABRAIN_WIZARD_INDEX_ENRICH_CAP_USD` |
| 7 | Auto-write Claude Desktop config (macOS/Linux) | PR-2 |
| 8 | Persist `ANTHROPIC_API_KEY` to `.env` | PR-2 |

## Scope — what changes

### Wizard surface (`init`)

| Item | Files | LOC |
|---|---|---|
| New stage 0 `_stage_setup` (fork prompt + Docker preflight + URL/key prompts) | `schemabrain/setup/setup_stage.py` (new), `schemabrain/setup/wizard.py` | ~150 |
| Sub-step Option B (Live elapsed timer + cost) in stages 3+4 | `schemabrain/setup/wizard.py`, `schemabrain/_ui.py` | ~80 |
| Snippet `env`-block URL injection | `schemabrain/setup/init_flow.py` | ~30 |
| Wizard exit panel: discovery links | `schemabrain/setup/wizard.py` | ~25 |

### Shared prompt helpers (one extract, reused 6 ways)

| Item | Files | LOC |
|---|---|---|
| `resolve_url_or_prompt()` + `resolve_anthropic_key_or_prompt()` | `schemabrain/_env.py` | ~80 |
| Silent `postgresql://` → `postgresql+psycopg://` rewrite | `schemabrain/_env.py`, `schemabrain/cli.py` | ~5 |
| Wire helpers into `index`, `check`, `entities suggest`, `metrics suggest`, `joins suggest` | `schemabrain/cli.py` | ~30 |

### Discovery surfaces

| Item | Files | LOC |
|---|---|---|
| `inspect` next-steps panel at bottom | `schemabrain/inspect/render.py` | ~20 |
| `doctor` no-store-yet branch | `schemabrain/setup/doctor_flow.py`, `schemabrain/setup/doctor_render.py` | ~15 |

### Tests

| Item | Files | LOC |
|---|---|---|
| Prompt-skips-in-yes-mode (Persona C gate) | `tests/test_setup_init.py`, `tests/test_cli_*.py` | ~80 |
| Prompt-fires-in-tty (mock isatty) | new `tests/test_env_prompts.py` | ~60 |
| Option B render snapshot | `tests/test_ui_primitives.py` (extend) | ~40 |
| Snippet `env` block contains URL | `tests/test_init_flow.py` | ~20 |

**Total: ~610 LOC code + ~200 LOC tests = 5–7 commits.**

## Scope — what does NOT change

- **Visual polish on existing surfaces** — already shipped in PRs #65–#78 design-system migration. Tree/Tables/Panels stay as-is.
- **`apply` commands** — already non-interactive, well-behaved.
- **`audit list` / `tail`** — local-only, no friction.
- **`*/list` subcommands** — local-only.
- **`serve`** — invoked by hosts, never typed.
- **`mcp install/uninstall`** — separate flow.
- **No new top-level commands** — no `schemabrain demo` / `schemabrain bootstrap` sibling.

## Engineering constraints (from audit)

These MUST be respected; ignoring them breaks the wizard contract or introduces UX regressions:

1. **Stage handlers MUST NOT raise.** Every new I/O op (Docker probe, prompt cancel, fixture-load error) translates to `StageOutcome(status="failed", next_step="...")`. Invariant-checked at `wizard.py:132-136`.
2. **Spinner-pause registry must wrap every interactive prompt.** Use existing `pause_active_spinner` (`_ui.py:508`) or the spinner-bleed bug recurs.
3. **TTY gate is `_stderr_is_interactive_tty()`** (`cli.py:5443`). Every prompt must early-return when this is False.
4. **`--yes` / `--non-interactive` are non-negotiable.** A test must assert zero stdin reads under these flags.
5. **Credential redaction precedent** — `password=True` on `Prompt.ask` for URLs and keys; no log echoes; reuse `_redact_env_args` pattern (`cli.py:5463-5485`).
6. **Host snippet `env`-block injection** — when URL is collected interactively, write it to the snippet's `env` block. Preserves env-var indirection (no URL in argv) while solving Claude Desktop cold-start.

## Implementation order (commits)

1. **Commit 1 — Planning doc.** This file.
2. **Commit 2 — Shared helpers in `_env.py`.** `resolve_url_or_prompt`, `resolve_anthropic_key_or_prompt`, silent `+psycopg` rewrite. With tests.
3. **Commit 3 — Snippet `env`-block injection.** `init_flow.build_snippet` accepts an `inline_url` arg; when present, writes `env: { SCHEMABRAIN_DATABASE_URL: <url> }` into the snippet. With tests.
4. **Commit 4 — Stage 0 `_stage_setup`.** New module `schemabrain/setup/setup_stage.py`. Fork prompt + Docker preflight + URL prompt + API key prompt with cost disclosure. Wire into `DEFAULT_STAGES` as stage 0. With tests.
5. **Commit 5 — Sub-step Option B.** Rich `Live` line with elapsed timer + cost, replacing the rapid-flash 3-label pattern in `_run_entity_suggestion` + `_run_metric_suggestion`. With tests.
6. **Commit 6 — Wire helpers into 5 post-init commands.** `index`, `check`, `entities suggest`, `metrics suggest`, `joins suggest`. With tests.
7. **Commit 7 — Discovery surfaces.** Wizard exit panel + `inspect` next-steps + `doctor` no-store branch.
8. **Commit 8 — Round-2 reviewer folds.** 3-agent rotation findings.

## Quality gates

Per `feedback_manual_smoke_mandatory.md`:

- All commits → `ruff check` clean, `ruff format` clean
- `pytest` green (no `-k`; full suite)
- Coverage ≥ 80% on touched modules
- 3-agent reviewer rotation (python-reviewer + silent-failure-hunter + Reality Checker) before push
- **Mandatory manual end-to-end smoke** against real Postgres before PR opens — recipe:
  1. Fresh venv: `python -m venv .venv-pr1 && source .venv-pr1/bin/activate && pip install -e .`
  2. Fresh Postgres: `docker compose -f docker-compose.yml up -d`
  3. Walk demo path: `schemabrain init` → pick `[2]` → confirm prompts → verify exit panel
  4. Walk own-DB path: `unset DATABASE_URL ANTHROPIC_API_KEY && schemabrain init` → pick `[1]` → paste URL → paste key → verify
  5. Walk friction commands: `schemabrain index` (no env), `schemabrain check` (no env), `schemabrain entities suggest` (no env) → verify each prompts
  6. Verify Claude Desktop config has `env.SCHEMABRAIN_DATABASE_URL` populated
  7. Verify `--yes` mode runs zero prompts: `SCHEMABRAIN_DATABASE_URL=... ANTHROPIC_API_KEY=... schemabrain init --yes` (CI shape)

## Out of scope — PR-2 backlog

- Auto-`docker compose up` from inside stage 0 (with health check, port-conflict probe, reuse-if-running)
- `schemabrain demo stop|status` cleanup commands
- Auto-write Claude Desktop config with `.bak` backup + diff preview
- Persist creds to `.env` with explicit consent + `.gitignore` check
- Windows Claude Desktop config support

## Sequencing with current branch

1. **Current branch first**: `fix/quickstart-honesty-and-host-snippet` (5 commits, awaiting user live test). This PR-1 work happens on a parallel branch off `main` so it doesn't block user live-test.
2. **If current branch lands first**: PR-1 rebases onto new main; the wizard.py sub-step labels from commit `4c9453b` are intentionally REPLACED by Option B in PR-1 commit 5.
3. **If PR-1 lands first**: current branch rebases; its sub-step labels commit becomes a no-op / gets dropped during rebase.
4. **No conflicts expected on `_env.py`, `_ui.py`, `setup/init_flow.py`** between the two branches — they touch different lines.
