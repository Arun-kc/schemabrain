---
title: "Setup"
description: "Pick a host, run the wizard, ask the agent — about 60 seconds end-to-end on a warm cache."
---

# SchemaBrain — Setup

The recommended path from "I have a Postgres database" to "an AI agent can answer questions about it" is the activation wizard (`schemabrain init`). It runs the source check, indexer, entity suggestion, and host wiring in one command.

Two alternative install paths are documented separately:

- [Docker](/setup/docker) — published image with the embedding model baked in (no first-run download).
- [Manual flow](/setup/manual) — explicit `index` invocation, logs config, troubleshooter, MCP Inspector, and an SQL-validation ladder.

## 1. Install + run the wizard

```bash
uvx schemabrain init
# or install first: pipx install schemabrain (or: pip install schemabrain)
```

That's it. With no URL set, the wizard opens with a fork prompt:

```
SchemaBrain — wire Claude to a Postgres database

How would you like to start?

  1. Connect my own Postgres
  2. Try with sample data (uses Docker)   ← default

Choice [2]:
```

- **Option 2** (default — press Enter) — spins up a local demo Postgres container (`sb-demo-pg` on port 5433) with the bundled SaaS fixture. Idempotent on re-runs. Requires Docker.
- **Option 1** — prompts for a `postgresql+psycopg://user:pass@host:5432/dbname` connection string (password-masked paste).

On the **demo path**, entities + metrics + joins are pre-applied from a bundled YAML pack — the semantic layer works zero-config, no `ANTHROPIC_API_KEY` required.

On **your own database**, exporting `ANTHROPIC_API_KEY` unlocks the entity + metric suggestion stages; without it the wizard soft-skips them gracefully — you can run `schemabrain entities suggest --apply` later once you have a key.

After URL resolution, the wizard shows a host-selection menu listing all 4 supported MCP hosts (Claude Desktop, Claude Code, Cursor, Windsurf) plus a Manual paste option. Detected installs get a ✓ chip; the default cursor points at the priority winner (Claude Desktop → Claude Code → Cursor → Windsurf). Press Enter to take the detected default, or type a number to pick something else. Pass `--host X` explicitly to bypass the prompt entirely; under `--yes` / non-TTY the wizard collapses silently to the priority-winner detection with a one-line stderr note. (For ChatGPT see the [Codex CLI page](/setup/codex) — there's no first-party `--host chatgpt` flag yet.)

```
SchemaBrain init — activation wizard

  [1/7] Source check       ✓ source reachable + read-only
  [2/7] Index schema       ✓ 12 tables, 84 columns indexed
  [3/7] Curate entities    ✓ 12 entities applied (bundled demo pack)
  [4/7] Curate metrics     ✓ 5 metrics applied (bundled demo pack)
  [5/7] Curate joins       ✓ 8 canonical joins applied (bundled demo pack)
  [6/7] Wire host          ✓ wrote schemabrain entry to claude_desktop_config.json
  [7/7] Next               ✓ restart your MCP host, then ask: "list the entities SchemaBrain knows about"
```

<Warning>
  **Connection scheme:** when you paste your own URL it **MUST** use `postgresql+psycopg://` (psycopg v3). A bare `postgresql://` is auto-rewritten with a one-line confirmation.
</Warning>

### Skip the prompts with a `.env` file

If you already have credentials in hand, drop them into a `.env` file in your working directory before running `init`. Create or edit `.env` with your editor of choice — the wizard reads these two keys:

```dotenv
SCHEMABRAIN_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
ANTHROPIC_API_KEY=sk-ant-...
```

Then:

```bash
schemabrain init
```

The CLI auto-loads `.env` from cwd before every subcommand. Shell exports always win — `.env` only fills gaps. If you paste your `ANTHROPIC_API_KEY` during the wizard instead, it offers to save the key back to `.env` for next time (opt-in, with a gitignore safety check).

<Note>
  Working in the source repo? Run `cp .env.example .env` once to seed the file with placeholder rows + comments, then fill in the values. The CLI also reads `SCHEMABRAIN_LOG_LEVEL` for log verbosity (`DEBUG` / `INFO` / `WARNING` / `ERROR`). **Do not** use `cat > .env` after copying — it would wipe the template.
</Note>

<Tip>
  **Scripted / CI runs:** for fully non-interactive invocations, combine `.env` with `--yes` to skip every prompt (host overwrite, LLM cost-cap pause). Full flag reference is in the [manual flow](/setup/manual) page.
</Tip>

### Install from source (alternative)

```bash
git clone https://github.com/Arun-kc/schemabrain && cd schemabrain
uv sync --extra dev
cp .env.example .env  # then fill in the keys

uv run schemabrain init
```

## 2. What the wizard does

Seven stages, ~45 seconds end-to-end on a warm cache:

1. **Source check** — validates the URL is reachable + read-only on Postgres. Auto-detects a dbt manifest from `$DBT_PROJECT_DIR/target/manifest.json` or by walking up from cwd for a `dbt_project.yml`. When found, stages 3 + 4 route through the dbt importer instead of the LLM. Force a manifest with `--from-dbt PATH`.
2. **Index schema** — DDL introspection into `./schemabrain.db`. Cost-free by default; `--enrich` opts in to LLM column descriptions ($0.10–$2.00 for a 50-table schema).
3. **Curate entities** — Claude Sonnet 4.6 proposes domain entities (or the dbt manifest is the source of truth when detected). Soft-skips if `ANTHROPIC_API_KEY` is absent. Cap spend with `--entities-max-cost-usd N`. Opt out with `--no-entities`.
4. **Curate metrics** — Claude Sonnet 4.6 proposes aggregations anchored on the curated entities (or dbt metrics are imported). Cap spend with `--metrics-max-cost-usd N`. Opt out with `--no-metrics`.
5. **Curate joins** — mines FK constraints + `pg_stat_statements` query log to surface canonical joins. Deterministic — no LLM call, no cost cap. Opt out with `--no-joins`.
6. **Wire host** — writes `schemabrain` into your MCP host's config. Use `--host claude-code` / `--host cursor` / `--host windsurf`, or `--host manual` (alias `--print-only`) for any other host — paste-ready snippet to stdout.
7. **Next step** — prints what to ask the agent first.

Stages 1, 2, 6, 7 abort on failure. Stages 3, 4, 5 are best-effort — a failure records the issue and prints a guided next step, but doesn't abort the wizard.

Before each LLM-driven stage (entities + metrics), the wizard pauses for Enter-to-continue with the cost cap formatted in the prompt. Skip the pause in scripted runs with `--skip-llm-confirm`; the full superset `--yes` skips both the LLM pause and the host-overwrite prompt. The pause auto-suppresses in non-TTY environments (CI, pytest).

Re-runs are idempotent: every stage auto-skips when its work is already done. Use `--skip-index` to opt out of stage 2, `--no-entities` / `--no-metrics` / `--no-joins` to opt out of stages 3 / 4 / 5 individually.

## 3. Open the dashboard

```bash
schemabrain dashboard
```

A localhost-only Next.js UI on `http://localhost:7878` showing PII categorization, refusal audit, and the audit chain status. See the [Dashboard overview](/dashboard/overview).

## 4. Wire your host

Per-host setup pages (~60-second wiring each):

| Host | Platform notes |
|---|---|
| [Claude Desktop](/setup/claude-desktop) | macOS + Windows |
| [Claude Code](/setup/claude-code) | CLI / all platforms |
| [Cursor](/setup/cursor) | Cursor IDE |
| [Windsurf](/setup/windsurf) | Windsurf / Cascade |
| [ChatGPT](/setup/chatgpt) | Codex CLI (stdio) |

## What's next

- [First 5 queries](/first-5-queries) — exercises each load-bearing firewall property in ~10 minutes.
- `schemabrain eval` — score retrieval quality against the bundled SaaS golden set (or your own).
- Re-run `schemabrain index` whenever your schema changes — it's idempotent and cache-aware.
- For schemas with cryptic column names (`acct_dim_v3`, `pmt_fct_h`), pass `--enable-sonnet` at index time to route those to Claude Sonnet 4.6 for better decoding (~5x cost per affected column).
