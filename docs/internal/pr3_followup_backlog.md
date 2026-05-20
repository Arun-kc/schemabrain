# PR-3 follow-up backlog — UX + new-contributor onboarding gaps

**Source**: parallel audits run on 2026-05-20 by two independent agents
(UX Researcher + Codebase Onboarding Engineer) against `feat/post-pr79-polish-bundle`
HEAD `a19cdf6` (PR-2 + D4-fix). Captured here so the 27 remaining findings
don't get lost between PR-2 push and PR-3 kickoff.

**Status of the 3 small fixes folded into PR-2 itself**:

- ✅ `SECURITY.md` version table refresh (Onboarding #5)
- ✅ `docs/architecture.md` "What's validated" refresh — date, version, tool count, test count (Onboarding #11)
- ✅ `_render_closing_block` shows config path so operator knows where the entry landed (UX #12)

Plus the implicit fix already in PR-2 itself:

- ✅ `.env` seeded from `.env.example` template on fresh write (D4-fix at `a19cdf6` — surfaced during step 5 of `docs/internal/manual_test_recipe_pr2.md`)

The 1 finding **rejected as invalid** (after code trace):

- ❌ UX #6 ("re-runs ask for the API key twice") — `cli.main()` calls `load_env_file_into_environ` BEFORE `_dispatch`, so `_resolve_anthropic_key_source` sees the loaded key on subsequent runs and short-circuits. The "consent fires after paste" claim is correct shape; the "re-runs ask twice" claim is wrong.

Everything else remains for PR-3+.

---

## Convergent findings (both audits caught the same shape independently)

**Convergent A — Spec/doc drift is real and pervasive.**
Same pattern across SECURITY.md, architecture.md, README's `pip install` claim
(v0.3.0 not yet on PyPI), `cli.py` being 7,277 lines vs the 800-line cap
declared in CONTRIBUTING.md. Three of these resolved by the fold above; the
PyPI publish + cli.py split are tracked below.

**Convergent B — The "easy path" is hidden.**
UX agent: README headline leads with `--url-env DATABASE_URL` instead of
the bare `schemabrain init` (which now auto-spins demo Postgres). Onboarding
agent: zero `good first issue` labels, no roadmap, no contribution recipes.
Same shape: the project has done the work but hasn't surfaced the affordance.

---

## End-user UX backlog (14 findings — UX #12 folded into PR-2)

### Critical

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| UX-1 | README headline command demands a working Postgres URL — but `schemabrain init` bare now auto-spins demo via D2. Headline hides the marquee feature. | Flip headline to `pip install schemabrain && schemabrain init`. Move `--url-env` form to a "Have your own DB?" subsection. | XS doc |
| UX-2 | Closing block says "Restart Claude Desktop" without ⌘Q emphasis. Most users will Cmd+W and conclude it's broken (MCP config only re-reads on cold start). | `_render_closing_block`: add `⚠ ⌘Q required — close-window doesn't trigger config reload`. Platform-detect modifier (Cmd vs Ctrl). | S |

### High

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| UX-3 | `postgresql+psycopg://` exposed in 3 doc surfaces; silent rewrite already exists internally. Forces users to learn an SQLAlchemy quirk. | Apply `silent_rewrite_to_psycopg` at every URL-accepting boundary (sweep the call-sites). Revert all public-facing examples to bare `postgresql://`. | S |
| UX-4 | `--url-env` / `--env-var` / `--source` is concept overload. Three flag names for one mental concept ("the env var holding my DB URL"). | Rename to `--from-env DATABASE_URL` OR auto-read `DATABASE_URL` from env when no flag given. Either deprecates the rest. | M |
| UX-5 | Stage 0 menu has no "I already exported DATABASE_URL" escape hatch — power users see the menu anyway. | In `_cmd_init` pre-stage-0: check `os.environ.get("DATABASE_URL")`, auto-use with `◇ Using DATABASE_URL from env` confirmation. | S |
| UX-6 | (REJECTED — see top of doc.) | — | — |

### Medium

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| UX-7 | Wizard's stage-pause shows neither cost preview nor Live elapsed timer, but standalone `entities suggest` shows both. Inconsistent with what the same user just saw. | Route `_prompt_llm_confirmation` through `print_llm_stage_preamble` + wrap call in `live_llm_stage_progress`. | S |
| UX-8 | "cancelled no changes made." is too terse. User doesn't know if the rest of the wizard ran or what to do next. | Expand to `cancelled · no changes made to <config path>. Re-run with --print-only to copy the snippet, or --yes to auto-accept.` | XS |
| UX-9 | Stage 0 demo path hardcodes port `5433`. Collides with anyone running another container on 5433. | Pre-check `socket.bind('127.0.0.1', 5433)`; if taken, increment to 5434/5435 and rewrite both the docker-run command and DEMO_DATABASE_URL in lockstep. | S |
| UX-10 | Bundled fixture path resolution uses `Path.cwd() / DEMO_FIXTURE_RELATIVE_PATH`. A pip-installed user has no repo on disk; gets `Fixture not found at /Users/me/Projects/.../ecommerce.sql`. | Resolve via `importlib.resources.files("schemabrain.eval.fixtures") / "ecommerce.sql"` (the same path the existing `fixture-path` command uses). | XS |
| UX-11 | README "Sample session" SQL joins `orders` × `order_items` × `categories`, but the bundled fixture has zero rows in `orders` / `order_items` (per smoke-2026-05-19 finding S5). First metric returns null. | Seed bundled `ecommerce.sql` with the 3-order dataset that `docs/setup.md:482-487` already inserts manually. | XS |
| UX-12 | ✅ FOLDED into PR-2 (`_render_closing_block` shows config path). | — | — |

### Low

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| UX-13 | `_PII_MARKERS` is color-only severity (red `pii`, yellow `internal`, dim `public`). Charter says glyph-first but `pii_marker` ships only a colored label. | Add per-tier glyph (`▲ pii`, `▲ confidential`, `▸ internal`, `· public`) matching the rest of the design system. | XS |
| UX-14 | `live_llm_stage_progress` elapsed timer ticks from 0s with no signal whether the SDK call was actually sent (vs still bootstrapping). | Print `→ request sent` after the SDK's HTTP request begins; Anthropic SDK exposes a callback for this. | S |
| UX-15 | README troubleshooting block at `:282` and `_render_pending_entity_block` use slightly different copy for the same "no entities" recovery. | Unify on the pending-entity copy (more specific + friendlier); link README to it directly. | XS |

---

## New-contributor onboarding backlog (14 findings — Onboarding #5 + #11 folded into PR-2)

### Critical

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| OB-1 | Zero `good first issue`-labeled work in GitHub. `gh issue list --state all` returns ONE closed issue. No backlog signal, no roadmap. Motivated contributors bounce. | File 5–10 genuinely small issues today (examples: "Windows path support in `setup/hosts.py`", "macOS x86_64 CI cell", "add `match_quality` enum from architecture.md §retrieval"). Label `good first issue`. | S |

### High

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| OB-2 | `CONTRIBUTING.md` has no "Where to contribute" section — covers HOW but not WHAT. | Add §"Where to contribute" with three tiers: (a) `good first issue` labels, (b) areas explicitly accepting help (new connectors? hosts?), (c) areas locked by maintainer (charter, audit row schema). Point to `docs/adr/` for "why" trails. | S |
| OB-3 | No "Adding a new MCP tool" recipe. Contributor has to reverse-engineer from `mcp/server.py` + `mcp/envelope.py` + `mcp/shapes.py` + 22KB charter. | Add `docs/contributing/adding-an-mcp-tool.md` with 6-step recipe: pick `tools/` entry, return shape from `envelope.py`, register in `server.py`, write tests against in-memory store, add to `mcp-tools.md`, run `charter_lint.py`. | M |
| OB-4 | No "Adding a new host" or "Adding a new connector" recipe. README hints Cursor/Continue/Windsurf but the Protocol contract isn't documented. | Add `docs/contributing/adding-a-host.md` + `adding-a-connector.md` pointing at the right Protocol/dataclass extension points. | M |
| OB-5 | ✅ FOLDED into PR-2 (`SECURITY.md` version table). | — | — |
| OB-6 | Reviewer-rotation discipline + 7-PR design-arc convention lives ONLY in `docs/internal/` and commit messages. New contributors will violate cemented conventions and not understand pushback. **User constraint**: keep `docs/internal/` internal. | LIFT the contributor-relevant rules into `CONTRIBUTING.md` (which is public): the 3-agent reviewer rotation expectation for PRs >100 LOC, the design-system glyph contract, the "no new public surfaces without an ADR" rule. Internal planning + smoke artifacts stay internal. | S |

### Medium

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| OB-7 | No `CODE_OF_CONDUCT.md`. GitHub community profile auto-detects this; strangers have no expectation set. | Add Contributor Covenant 2.1 (no-brainer default). Link from README + CONTRIBUTING. | XS |
| OB-8 | No discussion forum / Discord / community channel. `.github/ISSUE_TEMPLATE/config.yml` explicitly leaves `contact_links` empty. Non-bug questions have nowhere to go. | Either enable GitHub Discussions and link from config.yml, OR add a `question.yml` template that doesn't demand a repro. | XS |
| OB-9 | `docs/agent-ux-charter.md` is reviewed against every MCP-touching PR but never linked from `CONTRIBUTING.md`. Contributors will fail review for charter violations they didn't know about. | Add line to `CONTRIBUTING.md` §"Architecture invariants": "MCP tools must conform to the Agent-UX Charter; run `python scripts/charter_lint.py` before pushing." | XS |
| OB-10 | `schemabrain/cli.py` is 7,277 lines — 9× the 800-line hard cap declared in `CONTRIBUTING.md:91`. Contributors see the violation, lose trust in the rules. | Short-term: revise the cap with an explicit grandfather note ("`cli.py` is the only legacy file > 800 lines; new code stays under cap; splitting tracked at #N"). Long-term: actually split `cli.py` by subcommand group. | S note / L split |
| OB-11 | ✅ FOLDED into PR-2 (`docs/architecture.md` "What's validated" refresh). | — | — |
| OB-12 | README headline `pip install schemabrain` claims v0.3.0 — but PyPI publication was deferred per MEMORY.md. A literal-follower gets `No matching distribution found`. | Either publish to PyPI immediately (true fix) OR change install command to `pip install git+https://github.com/Arun-kc/schemabrain.git@main` until tagged. | XS doc / S publish |
| OB-13 | `docs/setup.md:8-9` framing of wizard-vs-manual is ambiguous. Contributors looking for "how do I run this against my dev DB" don't know which section to follow. | Move §0a (manual flow) below §0b (Docker); prefix with "If the wizard worked for you, skip this section." | XS |

### Low

| # | Finding | Fix shape | Effort |
|---|---------|-----------|--------|
| OB-14 | `examples/anthropic_demo.py` is unreferenced from README and CONTRIBUTING. Contributors wanting "how do I use this in my own code, not Claude Desktop" never discover it. | Link from README §"How it fits" and from `docs/setup.md` §"Anthropic SDK demo". | XS |
| OB-15 | `.review/` (PR-review rotation outputs per `.gitignore:88`) is gitignored. Institutional memory of "what reviewers caught and why" stays on the maintainer's laptop. | Either un-ignore and curate a `docs/review-archive/` of folded findings, OR write a single `docs/contributing/common-review-findings.md` distilling top-20 patterns from the 7-PR arc. | S distillation |

---

## Recommended PR-3 sequence

**PR-3a — Doc & contributor surface (low risk, high signal, ~1 day)**

Bundle these XS/S findings as a single doc-focused PR:

- UX-1, UX-15 (README + copy unification)
- OB-1 (file 5–10 `good first issue` tickets — do this BEFORE the PR so the README can link to a live backlog)
- OB-2 (CONTRIBUTING.md "Where to contribute")
- OB-6 (lift reviewer-rotation rule into CONTRIBUTING.md)
- OB-7 (CODE_OF_CONDUCT.md)
- OB-8 (GitHub Discussions enable + config.yml link)
- OB-9 (link charter from CONTRIBUTING.md)
- OB-12 (README install command honesty — either publish 0.3.0 to PyPI or document git-install)
- OB-13 (setup.md ordering)
- OB-14 (link `examples/anthropic_demo.py`)

**PR-3b — UX polish round (S/M, ~2 days)**

- UX-2 (Cmd+Q emphasis + platform detect)
- UX-5 (stage 0 escape hatch for already-exported DATABASE_URL)
- UX-7 (wizard stage-pause = standalone suggest preamble + timer)
- UX-8 (cancelled-message expansion)
- UX-9 (port collision detection)
- UX-10 (bundled fixture path via `importlib.resources`)
- UX-11 (seed bundled fixture with 3-order dataset)
- UX-13 (PII marker glyphs)
- UX-14 (`→ request sent` waypoint)

**PR-3c — Contributor recipes (M, ~2 days)**

- OB-3 (adding-an-mcp-tool.md)
- OB-4 (adding-a-host.md + adding-a-connector.md)
- OB-15 (common-review-findings.md distillation)

**PR-3d — `--url-env` rename / auto-detect (M+, ~3 days, needs design RFC)**

- UX-4 (rename or auto-detect)

This is the only finding that warrants design discussion before code change — affects every CLI surface + every external doc. Open as a draft RFC issue first, get sign-off, then implement.

**Deferred / hard splits**

- OB-10 cli.py split (L) — pure tech debt; tackle when a new subcommand group surfaces the seam naturally.
- UX-3 `postgresql+psycopg://` sweep (S) — sounds simple but the call-sites span tests; needs a careful audit + smoke.

---

## What this backlog deliberately does NOT include

- Performance work — neither audit surfaced it as friction
- Security findings — handled by the existing `security-reviewer` rotation
- Telemetry / analytics — no signal that operators want it
- Multi-user / team features — not in scope for the OSS layer
