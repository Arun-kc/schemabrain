# Schema Brain v0.3.0 new-user end-to-end smoke — 2026-05-19

**Tester:** Arun K C (with Claude Code)
**Build under test:** `schemabrain==0.3.0` installed via `uv pip install .` from `main` @ `a1a2939` (closest fidelity to a real `pip install schemabrain` without tagging + publishing first)
**Environment:** macOS arm64 / Darwin 24.6.0, Python 3.12.11, fresh venv at `/tmp/sb-smoke-2026-05-19-venv` (outside repo), npx 11.6.2, MCP Inspector via `npx @modelcontextprotocol/inspector --cli`
**Target:** bundled `schemabrain/eval/fixtures/ecommerce.sql` (7 tables, 30 cols) loaded into a fresh `smoke_2026_05_19` database on the running `sb-smoke-pg-2026-05-22` container (port 5434)
**Scope:** the full README + `examples/ecommerce/` walkthrough as a brand-new user would experience it — install → fixture load → `doctor`/`check`/`inspect` → `init` wizard (no-key path AND with-key+enrich path) → MCP Inspector → tool calls → `audit`/`tail`
**Cost:** $0.04 total for the with-key wizard ($0.0105 enrich + $0.0112 entities + $0.0184 metrics)

## Top-line verdict

**Would I tell a friend to install this today?** Almost — block on **B1** (PyPI publish) and **B2** (the `--source <VARNAME>` crash). Everything else is polish.

The two paths I walked both work end-to-end:

1. **Cost-free wizard** (`init --print-only --yes`, no `ANTHROPIC_API_KEY`): 7 stages run in 2.3s. Stages 3+4+5 cleanly skip with actionable cascade messaging ("entity store is empty; metrics need entities to anchor on"). The closing block correctly surfaces the "pending entity" branch that PR #52 introduced. Host snippet is well-formed.

2. **With-key + enrich** (`init --enrich --print-only --yes` with `ANTHROPIC_API_KEY` sourced from `.env`): 7 stages run in ~43s for $0.04 total. Stage 3 creates 6 entities, Stage 4 creates 10 metrics, Stage 5 emits 5 FK-derived joins. **PR #67's 300→4096 max_tokens bump is verified live — no truncation.** The entity/metric quality is striking: 6 entities named sensibly (address/category/order/order_item/product/user), 10 metrics with correct expressions and time-dim assignments (`sum(orders.total_cents)`, `avg(orders.total_cents)`, `count_distinct(orders.user_id)`).

The MCP-Inspector → 10 tools path works. `describe_table public.orders` returns full structure with 7 cols + 3 FKs + follow-up hints. **`get_metric registered_user_count time_grain=month` returns a fully-validated SQL skeleton** (`date_trunc('month', "user"."created_at")` with parameterized LIMIT and proper identifier quoting) that runs against the source and returns `[{time_bucket: '2026-05-01T00:00:00Z', registered_user_count: 3}]`. **No hallucinated SQL.** This is the product working as advertised.

The audit trail (`audit list` Rich table) captures every MCP call with status/cost/PII/fingerprint. Events JSONL captures lifecycle + tool-call events.

The findings below are 2 BLOCKERS, 6 SHOULD-FIX, and 3 NICE-TO-HAVE — all polish/edge-cases, no architectural defects.

---

## Findings — categorised

### BLOCKER (must fix before the next release announcement)

**B1. v0.3.0 not on PyPI; wizard's host snippet recommends `uvx schemabrain==0.3.0` which will fail for any real new user today.**
- File: snippet emitted by `schemabrain/wizard.py` (stage 6 host wire); also `examples/ecommerce/README.md`
- Repro: `pip install schemabrain` from a fresh shell → "Could not find version 0.3.0". Or pasted host snippet → MCP host can't launch schemabrain.
- Impact: a brand-new user following the README literally fails at install. Every demo, every Show HN, every "try it now" link is dead.
- Mitigation: tag `v0.3.0` and `uv build` + `uv publish` to PyPI. This is the only blocker between the current state and a real launch.

**B2. `schemabrain inspect --source <VARNAME>` AND `schemabrain check --source <VARNAME>` crash with an unhandled `ValueError` traceback when given an env-var name instead of a URL.**
- Files: `schemabrain/cli.py:5618` (`_cmd_inspect`), `schemabrain/cli.py:5528` (`_cmd_check`), root cause at `schemabrain/cli.py:5953` (`_canonical_url`)
- Repro: `schemabrain inspect --source DATABASE_URL` → 8-line traceback ending in `ValueError: Invalid connection URL (no scheme): 'DATABASE_URL'`. Same shape on `check`.
- Why this is a new-user trap: the alternative flag is `--url-env <VARNAME>` (e.g. `--url-env DATABASE_URL`). It is extremely intuitive to read `--source DATABASE_URL` as "use the URL named DATABASE_URL" since both `--source` and `--url-env` accept a single positional-looking value. The error message even reads as if `'DATABASE_URL'` is an INVALID URL — leaving the user wondering why a clearly-valid env var name "isn't a URL".
- Mitigation: wrap the `_canonical_url(source_url)` call in `_cmd_inspect` + `_cmd_check` in a try/except. On `ValueError`, emit the standard `error: ... why: ... fix: ... next:` block telling the user to either pass a real URL OR switch to `--url-env`. Localized 5–10 LOC fix per command.

### SHOULD-FIX (open issues; fold into v0.3.1 or v0.3.0 if still pre-publish)

**S1. `find_relevant_tables` returns `status: empty` with `follow_up_hints: null` when no descriptions are enriched. Silent dead-end for default-wizard users.**
- File: `schemabrain/mcp/tools/find_relevant_tables.py` (or wherever empty-result hints are assembled)
- Repro: run wizard WITHOUT `--enrich` (the documented "cost-free" path) → install MCP host → ask Claude "what tables have customer orders?" → Schema Brain returns `[]` with no hint. Claude has no follow-up to suggest. User sees nothing happen.
- Impact: the default new-user experience for one of the marquee tools is "type a question, get silence." Many users will not realize they need `--enrich` (which costs money).
- Mitigation: when `find_relevant_tables` returns empty AND the store has zero enriched descriptions, populate `follow_up_hints` with something actionable. Options:
  - Suggest `describe_table` if they know the table name (works without enrichment)
  - Hint that `schemabrain index --enrich` would unlock semantic search
  - Set status `degraded` instead of `empty` so the client surfaces a different message

**S2. `index` command surface is inconsistent with `check` / `inspect` / `init`.**
- File: `schemabrain/cli.py` (subcommand definitions)
- Repro: `schemabrain index --source DATABASE_URL ./schemabrain.db` → `error: unrecognized arguments: --source`. `index` accepts a positional `url` (deprecated) and `--url-env`. Every other command uses `--source URL` and `--url-env VARNAME`.
- Impact: users who learn one command's surface get tripped up moving to the next. Composability story breaks down for shell scripts.
- Mitigation: either add `--source URL` to `index` for parity (simplest), or deprecate `--source` everywhere in favor of `--url-env` (cleaner long-term — passwords-in-argv is a security smell anyway).

**S3. MCP tool calls silently drop unknown kwargs instead of rejecting them.**
- Repro: `tools/call get_metric name=total_revenue grain=month` → the tool succeeds and returns an UN-GROUPED query. The real arg name is `time_grain`. Pydantic accepted the extra `grain` field silently.
- Impact: agent quality issue. A model that fluent-mistakes `grain` for `time_grain` will get a query that doesn't bucket — silently wrong answers. The audit log records a successful call.
- Mitigation: configure the tool input models with `extra="forbid"` (Pydantic v2). On unknown kwarg, return a `status: error` with `kind: invalid_argument` and a `recovery.suggested_rewrite` listing the valid arg names.

**S4. Pydantic VALIDATION errors on MCP tool calls bypass the audit trail.**
- Repro: `tools/call suggest_joins qualified_name=public.orders` (wrong arg name) → MCP returns `isError: true` with the Pydantic validation message, but `audit list` shows NO row for this attempted call and `events.jsonl` has NO `tool_call` event.
- Impact: visibility gap. A sysadmin watching `audit list` or tailing events for malicious / broken clients won't see input-rejected calls — only the ones that reach tool code. Useful for both ops dashboards and for the "trust-the-agent" story (we ARE auditing every call... except the malformed ones).
- Mitigation: wrap the FastMCP tool dispatch in a layer that emits a `kind: tool_call, status: error, error_kind: invalid_argument` event BEFORE Pydantic validation fires. Or emit a separate event subtype `tool_call_rejected`. Either way, no MCP call should be unloggable.

**S5. Bundled ecommerce fixture has 0 rows in `orders` / `order_items` / `product_categories` — the marquee `total_revenue` metric returns `null`.**
- File: `schemabrain/eval/fixtures/ecommerce.sql`
- Repro: `get_metric total_revenue` → `{rows: [{total_revenue: null}], row_count: 1}`. Same for `order_count`, `average_order_value`, etc.
- Why this matters: `examples/ecommerce/README.md` Step 6 ("Ask Claude for a metric") expects users to see a real revenue number. They'll see `null` and assume the product is broken. The bundled fixture only seeds users (3), addresses (2), products (2), categories (3) — no transactional data.
- Mitigation: add a handful of `INSERT INTO public.orders (...) VALUES (...)` rows in the fixture, plus matching `order_items` and `product_categories`. ~10 lines of SQL. Goal: enough data that `total_revenue`, `order_count`, `distinct_ordering_customers`, `total_units_sold` all return non-null integers a user can sanity-check.

**S6. `find_relevant_entities` query="customer" surfaces `order` (0.685) above `user` (not in top 3). Semantic quality issue.**
- File: embedding store / entity description generation in `schemabrain/wizard.py` entity stage
- Repro: with 6 entities (address, category, order, order_item, product, user), query "customer" returns: order, product, category. `user` is the natural mapping for "customer" in this schema.
- Why: the entity has no LLM-generated DESCRIPTION embedded — only the name. Short names like `user` lose to longer compound names in cosine similarity. The wizard generates entity NAMES but doesn't (appear to) generate or embed a one-sentence description per entity.
- Mitigation: have stage 3 also generate a one-sentence description per entity (e.g. "user — registered account holder; the customer in this schema"), embed it, and use that for `find_relevant_entities` ranking. Marginal cost increase, big quality win.

### NICE-TO-HAVE

**N1. `doctor` doesn't notice when the host_config snippet's `store-path` references a different install location than the current cwd.**
- Repro: I'm running `doctor` from `/tmp/sb-smoke-2026-05-19-workspace` but the host config in `~/Library/Application Support/Claude/claude_desktop_config.json` was written by a prior install pointing at `/Users/arunkc/Codebase/schemabrain/schemabrain.db`. Doctor flags it as a warning (correct) but doesn't explain that the warning fires because Claude Desktop is talking to a DIFFERENT store than the one in the current workspace.
- Mitigation: when `host_config_store_path` warns, append a one-liner explaining "Claude Desktop will use the snippet's store; this workspace's store is separate."

**N2. `check` error message when `DATABASE_URL` is unset could hint at `--url-env DATABASE_URL`.**
- Repro: `schemabrain check --url-env DATABASE_URL` with the var unset → `error: environment variable 'DATABASE_URL' is not set. fix: export DATABASE_URL=postgresql+psycopg://...`
- The fix line tells the user to export the var. Good. But the error doesn't note that `--url-env` already accepted the var name correctly — the user just hasn't set it. Two-line clarification would resolve "is my flag wrong, or is my env missing?" ambiguity. Low priority.

**N3. Each MCP Inspector CLI invocation leaks a lingering `schemabrain serve` process. Events log shows 11 server_start / 10 server_stop after this smoke.**
- Repro: 11 invocations of `npx @modelcontextprotocol/inspector --cli ... schemabrain serve ...` → 11 starts logged, 10 stops logged. 1 server still running at the end. The inspector spawns serve, runs one method, and either SIGKILLs or the stop signal doesn't propagate cleanly.
- Impact: low. The processes are bounded — each one exits after stdin closes. But over a long debugging session, the leak adds up.
- Mitigation: likely an upstream MCP Inspector issue (worth filing). Defensive option on our side: add a stdin-close grace-timer in `schemabrain serve` so a missed close still results in clean exit within N seconds.

---

## What worked (PASS items worth documenting)

The point of the smoke is the findings list, but the PASS list is also evidence. Counted: **23 distinct PASS observations** across the install / wizard / inspect / MCP / audit / events surface.

**Install + pre-init UX:**
- ✅ Fresh `uv pip install .` → schemabrain==0.3.0 installed with ~50 deps, no warnings, no manual steps
- ✅ CLI on PATH after install; `schemabrain --version` reports `0.3.0`
- ✅ `schemabrain --help` lists 14 commands with one-line descriptions
- ✅ `doctor` (no env) runs without crashing, surfaces actionable warnings (7 pass / 4 warn / 0 fail)
- ✅ `check` (no `DATABASE_URL` set) emits clean `error: ... why: ... fix: ... next:` block
- ✅ `inspect` (no store) emits clean `error: store not found at ./schemabrain.db ... run schemabrain index ...`

**Wizard (no-key path):**
- ✅ `init --url-env DATABASE_URL --print-only --yes` runs all 7 stages in 2.3s
- ✅ Stage 1 (source check): Postgres reachable, **read-only session confirmed** — the "the agent never writes" guarantee gets surfaced from the wire
- ✅ Stage 2 (index): "7 tables · 30 columns indexed" in 0.6s
- ✅ Stage 3 (entities): clean skip with `↷` glyph + "ANTHROPIC_API_KEY not set; entity suggestion skipped" + actionable `export ANTHROPIC_API_KEY=...` hint — **PR #52 wizard-ordering fix verified**
- ✅ Stages 4+5 cascade correctly: "entity store is empty; metrics need entities to anchor on"
- ✅ Stage 6 (host wire): well-formed JSON snippet with version pin (`uvx schemabrain==0.3.0`), env block, absolute store path
- ✅ Stage 7 (next): "Ready" + closing-block with curate-NEXT recipes in dependency order (entities → metrics → joins)
- ✅ Tagline preserved at bottom of closing block: **"The agent reads. It doesn't write. That's the whole point."**

**Wizard (with-key + enrich path):**
- ✅ `init --enrich --print-only --yes` (key sourced from `.env`) runs all 7 stages in ~43s for $0.04 total
- ✅ Stage 2 enriches 30 columns for $0.0105
- ✅ **Stage 3 creates 6 entities for $0.0112 — PR #67's 300→4096 max_tokens bump verified live; no truncation**
- ✅ Stage 4 creates 10 metrics for $0.0184; metric expressions and time-dim assignments look correct
- ✅ Stage 5 emits 5 canonical joins from FK + query-log evidence (deterministic, no LLM cost)
- ✅ Closing block correctly switched from "pending entity" to "done" branch (the `_render_pending_entity_block` 4-way state machine PR #52 added)

**Post-enrichment inspect:**
- ✅ `inspect` (no source) shows full Tree: 6 entities + 10 metrics + 5 joins, each as a nested branch — PR #65's Tree renderer working
- ✅ `entities list` / `metrics list` / `joins list` show flat tabular form with origin=suggested annotations
- ✅ Quality of LLM output: entities map to tables cleanly (address→addresses etc.); metrics include `sum(total_cents)`, `count_distinct(user_id)`, `avg(price_cents)` with proper time_dim (`order.placed_at`, `user.created_at`) and grain support (day/week/month/quarter/year)

**MCP Inspector + tool calls:**
- ✅ `npx @modelcontextprotocol/inspector --cli ... schemabrain serve ... --method tools/list` returns **exactly 10 tools** matching the README claim (`find_relevant_tables`, `find_relevant_entities`, `describe_table`, `describe_column`, `get_example_queries`, `suggest_joins`, `list_entities`, `describe_entity`, `resolve_join`, `get_metric`)
- ✅ `describe_table public.orders` returns full structure (7 cols, 3 FKs, token_estimate=352, follow_up_hints=[describe_column, suggest_joins], confidence=HIGH)
- ✅ `find_relevant_tables "customer orders" limit=3` (post-enrich) returns 3 hits with cosine scores; `public.orders` correctly ranks #1 at 0.778
- ✅ `suggest_joins tables=["public.orders","public.users"]` returns 1-hop FK path via `orders_user_id_fkey` with confidence=1.0 and provenance=schema
- ✅ `get_metric registered_user_count time_grain=month` returns validated SQL `SELECT date_trunc('month', "user"."created_at") AS time_bucket, count("user"."id") AS "registered_user_count" FROM "public"."users" AS "user" GROUP BY time_bucket LIMIT :p_limit` with parameterized LIMIT — **and it runs against the source** returning `[{time_bucket: '2026-05-01T00:00:00Z', registered_user_count: 3}]`
- ✅ `get_metric` for an undefined metric returns charter-compliant `status: error, kind: unknown_metric` with `recovery.suggested_tool: list_entities`
- ✅ `list_entities` / `find_relevant_entities` both return clean empty-state with cascading `follow_up_hints` BEFORE entities curated, and clean populated state AFTER

**Audit + events:**
- ✅ `audit list` shows Rich table with 6 rows — one per MCP tool call — with `id / occurred_at / tool / status / cost / pii / fingerprint` columns
- ✅ Events JSONL at `~/.schemabrain/events.jsonl` (matches `tail` default) captures 21 server lifecycle events + 5 tool_call events across the smoke
- ✅ Read-only DB session is maintained: every successful MCP call hit the live DB, no INSERT/UPDATE/DELETE in the audit trail

---

## Smoke recipe (reproducible)

```bash
# 1. Setup
docker exec sb-smoke-pg-2026-05-22 psql -U postgres -c 'CREATE DATABASE smoke_2026_05_19;'
uv venv /tmp/sb-smoke-2026-05-19-venv --python 3.12
source /tmp/sb-smoke-2026-05-19-venv/bin/activate
uv pip install .   # from schemabrain repo root; proxies pip install schemabrain
docker exec -i sb-smoke-pg-2026-05-22 psql -U postgres -d smoke_2026_05_19 \
    < schemabrain/eval/fixtures/ecommerce.sql

# 2. Workspace
mkdir -p /tmp/sb-smoke-2026-05-19-workspace && cd /tmp/sb-smoke-2026-05-19-workspace
export DATABASE_URL='postgresql+psycopg://postgres:local@localhost:5434/smoke_2026_05_19'
# Optional: source .env to get ANTHROPIC_API_KEY
set -a; source /Users/arunkc/Codebase/schemabrain/.env; set +a
export DATABASE_URL='postgresql+psycopg://postgres:local@localhost:5434/smoke_2026_05_19'

# 3. Pre-init surface
schemabrain doctor
schemabrain check --url-env DATABASE_URL    # works
schemabrain inspect                          # clean "no store" error

# 4. Wizard (cost-free path; no key needed)
schemabrain init --url-env DATABASE_URL --print-only --yes

# 5. Wizard (full path; needs ANTHROPIC_API_KEY)
rm -f schemabrain.db   # clean for fresh run
schemabrain init --url-env DATABASE_URL --enrich --print-only --yes

# 6. Post-enrich inspect
schemabrain inspect --url-env DATABASE_URL
schemabrain entities list && schemabrain metrics list && schemabrain joins list

# 7. MCP Inspector — tools/list + a tool call
SCHEMABRAIN_DATABASE_URL="$DATABASE_URL" \
    npx -y @modelcontextprotocol/inspector --cli \
    /tmp/sb-smoke-2026-05-19-venv/bin/schemabrain serve \
    --url-env SCHEMABRAIN_DATABASE_URL --store-path ./schemabrain.db \
    --method tools/list

SCHEMABRAIN_DATABASE_URL="$DATABASE_URL" \
    npx -y @modelcontextprotocol/inspector --cli \
    /tmp/sb-smoke-2026-05-19-venv/bin/schemabrain serve \
    --url-env SCHEMABRAIN_DATABASE_URL --store-path ./schemabrain.db \
    --method tools/call --tool-name get_metric \
    --tool-arg 'name=registered_user_count' --tool-arg 'time_grain=month'

# 8. Audit + events
schemabrain audit list
tail -5 ~/.schemabrain/events.jsonl
```

## Recommendation

**Sequence for v0.3.0 publish + v0.3.1:**

1. **Before tagging v0.3.0 to PyPI:** fix B2 (the `--source <VARNAME>` crash). It's a ~10-line fix per command and saves any new user from a Python traceback on a trivial mistake. Optionally fix S5 (fixture inserts) since it's also tiny and improves the demo dramatically.
2. **Tag + publish v0.3.0 to PyPI** (B1). Then the README and `examples/ecommerce` walkthrough actually work.
3. **v0.3.1**: bundle the remaining SHOULD-FIX items (S1 silent dead-end on `find_relevant_tables`, S2 `index` surface inconsistency, S3 silent kwarg drop, S4 validation-error audit gap, S6 entity description embedding). These are all polish but compound — addressing them is the difference between "works" and "feels designed."
4. **NICE-TO-HAVE** items can go into v0.3.2 or be deferred indefinitely.

**Sustained PASS posture:** the product clearly works end-to-end. The validated-SQL story (`get_metric` returning a parameterized, properly-quoted, GROUP BY'd, time-bucketed query that actually runs against the source) is the load-bearing promise of the platform, and it does what it says.
