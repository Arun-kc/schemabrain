# v0.3.0 publish readiness report — 2026-05-20

**Internal doc.** Synthesizes a 4-agent rotation (Product Manager, Reality
Checker, Developer Advocate, Codebase Onboarding Engineer) + mechanical
checks (`uv build`, `git tag`, prepublication scrub grep, CHANGELOG audit)
against the question: **"are we ready to publish v0.3.0 to PyPI today?"**

---

## Verdict: **NO-GO today. ~6-8 hours of focused PR-3 work → GO.**

The product substrate is real. The 2026-05-16 Reality Checker "theatre"
warning is **CONFIRMED RESOLVED** — audit chain, PII classifier (12
regulator-derived categories), `--pii-block` refusal, validated SQL, dbt
importer, drift detection, 10 MCP tools all traced to actual shipping
code. The "agent never writes SQL" headline is structurally honest.

What's not ready is **everything around the product**: the README's
day-one walkthrough has falsifiable claims a new user catches in <5
minutes, the wheel won't build because of stray venvs in the worktree,
internal review-process attribution leaked into ~25 public code
comments, the CHANGELOG's v0.3.0 entry would misrepresent what's in the
wheel, and three install surfaces all depend on a PyPI version that
doesn't exist yet.

Path B from the PM verdict was right (publish after polish, not as-is,
not after a full PR-3/4/5/6 cycle) — but the scope is larger than PM
estimated. **6-8 hours, not 3-4.**

---

## Convergent findings (caught by 2+ agents independently)

These are the load-bearing fixes:

| # | Finding | Agents | Severity |
|---|---|---|---|
| 1 | `pip install schemabrain` returns 0.2.0a1, not 0.3.0 | PM + RC + DA + OB | **CRITICAL** |
| 2 | `--url-env DATABASE_URL` in headline command is outdated (wizard now self-prompts) | OB + my positioning audit | **HIGH** |
| 3 | `postgresql+psycopg://` footnote teaches a gotcha the silent rewrite fixed | OB + my positioning audit | **HIGH** |
| 4 | `examples/anthropic_demo.py` (pluggable proof) buried in "optional" subsection | DA + OB + my positioning audit | **HIGH** |
| 5 | Quickstart §2 4-command Docker dance now automated by stage 0 | OB + PM | **HIGH** |
| 6 | CHANGELOG `[Unreleased]` content (PRs #65-#80) not folded into v0.3.0 entry | PM + my mechanical check | **HIGH** |

---

## CRITICAL items (publish-blockers)

### Mechanical (caught pre-agent by `uv build` + git checks)

1. **`uv build` FAILS** — `.venv-pr1/` + `.venv-pr2-smoke/` at repo root get packed into sdist, hatchling chokes on absolute symlink to `python3.14`. Cannot publish until clean.
2. **`.gitignore` only excludes `.venv` (singular)** — needs `.venv*` glob OR explicit hatchling sdist exclude.
3. **~25 internal-process artifacts leak into public code** — violates the `feedback_prepublication_scrub.md` rule. Specific files (line refs):
   - `schemabrain/_ui.py:695, 728, 735, 839` — "Round-2 fold MED/CRITICAL (silent-failure-hunter / python-reviewer)"
   - `schemabrain/cli.py:5055-5056, 5113, 7096` — same pattern
   - `schemabrain/errors_render.py:449, 543` — "python-reviewer flagged convergently"
   - `schemabrain/setup/{init_flow,wizard,setup_stage}.py` — multiple Round-1/2 fold references
   - `schemabrain/mcp/{envelope,shapes,server,_helpers}.py` — "wk-11/12/13" milestone refs
   - `schemabrain/{core/entity,mining/__init__,eval/bundled,eval/fixtures/ecommerce.sql}.py` — "wk-N" refs
   - `CHANGELOG.md:865-866` — "wk-15"
4. **CHANGELOG has `[Unreleased]` ABOVE `[0.3.0] - 2026-05-18`** — needs folding (or new v0.3.1 cut).
5. **No git tag `v0.3.0` exists** — only `v0.1.0a1`, `v0.2.0a1`.
6. **Working tree not clean** — `M docs/internal/manual_smoke_2026_05_18.md`, `?? schemabrain-smoke.db`, `?? docs/internal/{manual_smoke_2026_05_19,positioning_audit_2026_05_20}.md`. Need to commit / discard / gitignore.

### Reality Checker — README claims that fail under inspection

7. **C1 — Sample-session column counts FABRICATED** ([README.md:56-63](../../README.md#L56-L63)). Says `users=12, products=7, product_categories=3`. Fixture truth: `users=4, products=5, product_categories=2`. Only `order_items=5` is correct. First-60-second bounce risk.
8. **C2 — Cost-table contradiction** ([README.md:70](../../README.md#L70) vs [docs/architecture.md:97](../../docs/architecture.md#L97)). README says "7 tables, 30 columns, ~$0.01 in ~40s." Architecture doc says "6 tables / 24 columns / $0.0074 / 38 sec / measured." Fixture truth: 7 tables / 30 columns. One source is wrong.
9. **C3 — Docker port contradiction** ([README.md:99](../../README.md#L99) uses `-p 5432:5432`, [docker-compose.yml:45](../../docker-compose.yml#L45) binds `127.0.0.1:5433:5432`). Two contradictory recipes; READMEs §2 vs §"Run via Docker" disagree.
10. **C4 — `ghcr.io/arun-kc/schemabrain` returns 404 Package not found** (live-verified via `gh api`). README [README.md:557](../../README.md#L557) + docker-compose.yml reference it. Publish workflow only pushes image when `target == 'pypi'` runs — hasn't yet.
11. **C5 — `schemabrain --version → 0.3.0` check** ([README.md:84](../../README.md#L84)) fails for PyPI users today (self-fixes after publish, but the contradiction lives during the publish window).

### Codebase Onboarding Engineer — the hidden 3-surface failure

12. **`uvx schemabrain==0.3.0` in Claude Desktop snippet** ([schemabrain/setup/hosts.py:130-132](../../schemabrain/setup/hosts.py#L130-L132)) — even after a successful local `pip install`, Claude Desktop boots the MCP server via uvx, which **fetches fresh from PyPI**. If v0.3.0 isn't on PyPI, Claude Desktop fails silently to start the server. Doctor reports green (only checks config file landed, not uvx resolution). User sees nothing in Claude UI. This is the biggest single bounce risk and was not in the original positioning audit.

### Developer Advocate — agent-integrator surface

13. **`anthropic_demo.py` uses `--source URL` while every other surface uses `--url-env VARNAME`** ([examples/anthropic_demo.py:110-117](../../examples/anthropic_demo.py#L110-L117)) — integrators cribbing the demo learn the deprecated flag.
14. **`describe_entity` returns `pii_sensitivity: "public"` hardcoded** ([docs/mcp-tools.md:213-218](../../docs/mcp-tools.md#L213-L218)) — integrators see every column as "public", conclude PII story is fiction. Ship real classification today OR hide the field until v2.

---

## HIGH items (publish anyway, fix in v0.3.1)

- **H1 (RC)** — Quickstart §3 wizard sample shows "3 entities" but inspect sample below shows "6 entities · 10 metrics · 5 joins". Pick one snapshot, stick with it.
- **H2 (DA)** — Claude Code wiring is one line ([README.md:177](../../README.md#L177)) with no worked example or troubleshooting block.
- **H3 (DA)** — Continue / Windsurf / Zed get one line between them. Three 18-line JSON snippets would close the "any host" promise.
- **H4 (OB)** — `examples/ecommerce/README.md:42-49` still teaches the pre-stage-0 Docker dance.
- **H5 (OB)** — `docs/setup.md` §0 still tells users to export `DATABASE_URL` before init.
- **H6 (OB)** — `_handle_demo_path` silently downgrades to `_handle_own_db_path` when Docker is missing — confusing pivot.
- **H7 (RC)** — Sample-session caveat "Prices live on `order_items.unit_price_cents`, not `orders`" is half-true (`orders.total_cents` also exists).

---

## What the audit CONFIRMS is REAL (the substrate story is honest)

The 2026-05-16 Reality Checker called audit/PII/drift "theatre." Today:

- ✅ **Tamper-evident audit log** — `sha256(prev_chain + canonical_row)` in `audit/chain.py:44`, walker in `audit/verify.py`, exits 0/1 per `cli.py:5323-5369`
- ✅ **PII classifier with 12 regulator-derived categories** — `schemabrain/pii/categories.py` enumerates exactly: `contact, financial, payment_card, health, genetic, biometric, behavioral, online_identifier, credential, government_id, location, demographic_protected`
- ✅ **`--pii-block` actually refuses** — `mcp/get_metric.py` emits `kind="pii_blocked"` envelope; SQL never compiles; audit row records `refusal_reason='pii_blocked'`
- ✅ **3 PII regexes in profiler/stats.py** — `_EMAIL_RE`, `_SSN_RE`, `_CC_RE` at lines 28, 35, 42
- ✅ **All 10 MCP tools registered** — `grep '@_trace' schemabrain/mcp/server.py` returns exactly the 10 listed in README
- ✅ **`--host claude-code` shells out to `claude mcp add`** — verified at `setup/hosts.py:9-10, 217`
- ✅ **dbt importer wired** — `--from-dbt`, `schemabrain import dbt`, `$DBT_PROJECT_DIR` auto-detect, `ecommerce_manifest.json` bundled fixture
- ✅ **Events JSONL with 10 MiB rotation** — `observability/bus.py:31` `DEFAULT_MAX_BYTES = 10 * 1024 * 1024`
- ✅ **`schemabrain check` drift detection** — wired and shape matches renderer

**The product is shippable.** The presentation is what needs work.

---

## PR-3 scope (publish-blocker remediation, ~6-8 hours)

Five tracks, can be one or two PRs depending on review appetite:

### Track A — Mechanical cleanup (~1 hr)
- Delete `.venv-pr1/`, `.venv-pr2-smoke/`, `schemabrain-smoke.db`
- Update `.gitignore`: `.venv*`, `schemabrain-smoke.db`, `*.tmp.db`
- Add `[tool.hatch.build.targets.sdist] exclude = [".venv*", "dist", ...]` to pyproject.toml as belt-and-suspenders
- Verify `uv build` succeeds cleanly
- Commit pending working-tree changes (manual_smoke_2026_05_19.md, positioning_audit_2026_05_20.md, this doc)

### Track B — Prepublication scrub (~1.5 hr)
- Remove ~25 internal-process attributions per `feedback_prepublication_scrub.md`
- Substantive WHY stays; "(silent-failure-hunter)" / "(python-reviewer)" / "Round-1/2 fold" / "convergent HIGH" / "wk-N" goes
- Run the canonical scrub grep AFTER edits to confirm zero remaining hits

### Track C — CHANGELOG reconciliation (~1 hr)
- Fold `[Unreleased]` (line 8-749) into `[0.3.0]`
- Re-date `[0.3.0]` from 2026-05-18 → 2026-05-20
- Reorganize merged v0.3.0 into clean `### Added` / `### Changed` / `### Fixed`
- Add 3-sentence "Highlights" paragraph summarizing shipped firewall properties for the GH Releases auto-body
- Add fresh empty `[Unreleased]` header at top

### Track D — README factual fixes (Reality Checker CRITICALs, ~1.5 hr)
- C1: Fix sample-session tail column counts (users=4, products=5, product_categories=2; order_items=5 was correct)
- C2: Reconcile architecture.md cost table to 7 tables/30 columns; measure once on a clean wizard run
- C3: Pick port 5432 in Quickstart §2 (matches `docker run -p 5432`). Clarify §"Run via Docker" uses port 5433 deliberately to avoid host clash. Two recipes, two ports, clearly labelled.
- C4 (decision): Either DELETE the ghcr.io claims OR ensure publish workflow runs the docker step on v0.3.0 publish AND verify image lands before announcing. **Recommend: delete claim from README, restore in v0.3.1 after first successful docker-publish run.**
- H1: Pick one snapshot (3 entities OR 6 entities) and reconcile across wizard sample + inspect sample

### Track E — README UX + positioning (~1.5 hr)
- Drop competing "schema intelligence" sub-headline ([README.md:9](../../README.md#L9))
- Headline command: bare `schemabrain init` (no `--url-env`)
- Drop `postgresql+psycopg://` footnote ([README.md:113](../../README.md#L113)) — replace with "Standard Postgres URLs work; we accept `postgresql://`, `postgresql+psycopg://`, and `postgres://`."
- Drop `schemabrain --version → 0.3.0` line ([README.md:84](../../README.md#L84)) — self-fixes after publish
- Promote `examples/anthropic_demo.py` to dedicated subsection after Quickstart §6 (5-line summary + the one-liner)
- Fix the `--source URL` → `--url-env DATABASE_URL` inconsistency in `anthropic_demo.py` itself
- Collapse Quickstart §2 (Docker) + §3 (init) into a single "Run `schemabrain init`" section. Add `<details>` block for users with their own DB.

---

## Publish-day sequence (user-owned)

After PR-3 lands + CI green:

1. User tags `v0.3.0` on GitHub with release notes covering PRs #51 → #80 (auto-generated from the CHANGELOG `[0.3.0]` block)
2. User cleans workspace: `rm -rf .venv-pr* dist/ schemabrain-smoke.db` (defensive — PR-3 Track A should have done this but verify)
3. User runs `uv build && uv publish` from a clean checkout of the tagged commit
4. **Wait ~15 min for PyPI search-index propagation. Do NOT announce yet.**
5. **CRITICAL**: From a fresh shell on a fresh box (or `--no-cache-dir`), verify:
   - `pip install schemabrain==0.3.0` resolves
   - `uvx schemabrain==0.3.0 --version` resolves (the hidden 3rd surface OB caught)
   - `pip install schemabrain` returns 0.3.0 (no version pin)
6. Verify ghcr.io image actually pushed by publish workflow (or confirm decision to defer Docker step)
7. Final pre-announce smoke: fresh venv → `pip install schemabrain==0.3.0` → `schemabrain init` (demo path, no API key) → `schemabrain doctor` → `schemabrain inspect`. Should be ~5 min total.
8. Announce.

---

## What's explicitly DEFERRED to v0.3.1

These ARE good improvements; they don't block publish:

- **Quickstart cut to 3 steps** (positioning audit PR-4) — TODO once §2/§3 collapse lands
- **Firewall property promotion above the fold** (positioning audit PR-5)
- **Continue / Windsurf / Zed worked examples** (DA H3)
- **`describe_entity` `pii_sensitivity` hardcoded "public"** (DA — needs product call: ship real or hide field)
- **`list_metrics` MCP tool** (DA — agents on fresh install hit `unknown_metric`)
- **`find_relevant_tables` empty-state hint to `--enrich`** (DA)
- **Doctor verification of `claude mcp list` for `--host claude-code`** (DA)
- **Wizard cost-preview live pause copy polish** (OB)
- **Stage 2 empty-schema distinct error** (OB)
- **`docs/integrations/` subdirectory per-host recipes** (DA — DevRel work)
- **`examples/openai_demo.py` + `examples/gemini_demo.py`** (DA — DevRel work)
- **All of original PR-3a contributor onboarding** (positioning shift, indefinite deferral)

---

## Decisions the user has to make

1. **ghcr.io claim**: delete from README until publish workflow runs the docker step? OR ensure docker step runs on first publish? **(Recommend delete; restore in v0.3.1.)**
2. **CHANGELOG `[0.3.0]` date**: 2026-05-18 (when PRs #48-#52 closed the original 12-gap audit) or 2026-05-20 (today, when the wheel actually ships)? **(Recommend 2026-05-20.)**
3. **Real PII classification on `describe_entity` vs hide the field**: ship today or defer? **(Recommend defer; honest disclaimer in mcp-tools.md until classifier hits the wire.)**
4. **PR-3 as one PR or split (A+B = mechanical+scrub, C+D+E = docs)**: one PR is faster to review; two is cleaner for the rebase. **(Recommend one PR — these changes are tightly coupled.)**

---

## Bottom line

The product is real and the substrate is honest. The 2026-05-16 theatre
concerns are resolved. The 4246-test / 99.02% coverage / 5 CI-green
baseline is genuine. But the README's day-one experience is 5+ minor
contradictions away from "obviously trustworthy," and the publish-day
mechanics (clean build, scrubbed code, consolidated CHANGELOG, tagged
release) are not yet in place.

**6-8 hours of focused PR-3 work converts NO-GO to GO.** Without it,
the publish-day failure mode is "the README lies in the first 5 minutes
and the discerning reader bounces." With it, the product is in shape to
earn the trust the positioning promises.
