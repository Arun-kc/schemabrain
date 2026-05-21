# PR-4 plan — Quickstart 7→3 step cut

**Status:** plan
**Branch (proposed):** `docs/pr4-quickstart-3-steps`
**Base:** `main` (HEAD after PR-3 / v0.3.0 publish lands)
**Scope:** README-only. No code changes.
**Estimated effort:** ~0.5 dev-day + manual smoke (~30 min).
**Predecessor:** PR-3 (install honesty, v0.3.0 on PyPI) — must be merged first.
**Successor:** PR-5 (promote firewall properties above the fold) and PR-6 (promote `examples/anthropic_demo.py`).

---

## Premise

The current README Quickstart at `README.md:76-285` is **7 numbered sections**:

| # | Current section | Lines |
|---|---|---|
| §1 | Install | 80–93 |
| §2 | Start Postgres and load the bundled fixture | 95–113 |
| §3 | Run the activation wizard | 115–179 |
| §4 | Confirm it's wired (`doctor`) | 181–187 |
| §5 | Restart your MCP host and ask the test question | 189–197 |
| §6 | See what got indexed (`inspect`) | 199–255 |
| §7 | Plug into your own agent loop (`anthropic_demo.py`) | 257–274 |
| (extra) | Inspect the MCP surface (MCP Inspector) | 276–285 |

**The wizard surface has moved past the README.** Two PR-family shipments make most of those sections obsolete:

- **PR-2 D2 (`88ae34a`)** — auto-`docker run` from stage 0 with idempotent container reuse, fixture autoload, safe fallback to the pre-D2 manual recipe on subprocess/readiness failure. **Eliminates §2** for the demo path.
- **PR #79 stage-0 fork (`d55f668` family)** — `schemabrain init` with no URL prompts the user "demo vs own-DB" interactively on stage 0. **Eliminates the URL/env-var pre-export in §3.**
- **PR-2 D4 (`d7cd4fb`)** — `.env` persist for `ANTHROPIC_API_KEY` with consent, gated on `_ui.offer_persist_anthropic_key_to_env_file` with three hard contracts (opt-in / no-silent-write / gitignore-warn). **Eliminates the API-key pre-export in §3.**

The README's own headline at `README.md:16-20` already promises the 3-step shape:

```bash
pip install schemabrain
schemabrain init
# then ask your MCP host: "list the entities Schema Brain knows about"
```

PR-4 makes the Quickstart section honest to that headline promise.

---

## Target state — 3 numbered steps

### 1. Install

```bash
pip install schemabrain
schemabrain --version
```

(Optional from-source paragraph stays inside the same §1.)

### 2. Run the activation wizard

```bash
schemabrain init
```

The wizard walks 7 stages end-to-end (~40s on the bundled demo, ~$0.04 with Claude). On first run it prompts for:

- **A Postgres URL** — paste your own connection string, or press Enter to spin up a local demo container with the bundled e-commerce fixture (`docker run` is auto-invoked; idempotent on re-runs).
- **An `ANTHROPIC_API_KEY`** — optional. Skip and the wizard still wires Claude Desktop; entity curation can run later via `schemabrain entities suggest --apply`.

The 7-stage progress bar (Source check / Index schema / Curate entities / Curate metrics / Curate joins / Wire host / Next) prints inline; `--yes` skips all interactive prompts for CI use.

### 3. Restart Claude Desktop and ask

1. **Cmd+Q** Claude Desktop fully — not just close the window. The MCP config is only read on cold start.
2. Relaunch.
3. New conversation: `list the entities Schema Brain knows about`

If Claude calls `list_entities` and reports `user`, `order`, etc., you're done.

---

## Section migration table — where current §s land

| Current section | Action | Destination |
|---|---|---|
| §1 Install | Keep, simplified | New Step 1 (drop the `schemabrain --version` confirmation? — TBD; keep for now) |
| §2 Start Postgres + load fixture | **DELETE** | Wizard handles via auto-Docker on stage 0 demo path; own-DB path skips entirely (user supplies URL when prompted) |
| §3 Run wizard | Collapse | New Step 2; drop env-var exports; drop `--url-env` / `--store-path` flags from headline command; keep the "What each stage does" detail block as `<details>` collapsible directly under Step 2 |
| §4 Confirm wired (`doctor`) | **MOVE** | New post-Quickstart section "Verify the install" OR fold into "If something went wrong" troubleshooter — leaning **fold into troubleshooter** since the wizard's stage 6 already verifies host wiring |
| §5 Restart Claude + ask | Keep, simplified | New Step 3 |
| §6 See what got indexed (`inspect`) | **MOVE** | New post-Quickstart section: "After the wizard — see what got indexed" |
| §7 Plug into your own agent loop | **MOVE** | New post-Quickstart section: "After the wizard — plug into your own agent loop" — note: PR-6 will promote this further; PR-4 just relocates, doesn't expand |
| Inspect MCP surface (extra) | **MOVE** | `docs/setup.md` (link from new post-Quickstart subsection) |
| Driver-prefix footnote at line 113 | Already shipped neutral in PR #81 — verify still neutral, no change | — |

**Net result:** Quickstart shrinks from ~210 lines (76–285) to ~60–80 lines. The relocated content stays in the README (just below the Quickstart) so PR-4 doesn't fragment the user journey across docs/ files.

---

## Proposed new section order (post-PR-4)

```
1.  Hero (title + tagline + 4 bullets)                  unchanged
2.  Headline install snippet (lines 16-22)              unchanged
3.  Sample session (lines 26-72)                        unchanged
4.  Quickstart                                          NEW SHAPE: 3 steps
      §1 Install
      §2 Run the wizard
      §3 Restart Claude and ask
5.  After the wizard                                    NEW SECTION
      - See what got indexed (was §6)
      - Plug into your own agent loop (was §7)
      - Inspect MCP surface (link to docs/setup.md)
6.  If something went wrong                             folded `doctor` recipe in
7.  What's next                                         unchanged
8.  Build your semantic layer                           unchanged
9.  Observe the agent                                   unchanged (PR-5 promotes pieces of this)
10. Operate over time                                   unchanged
11. How it fits                                         unchanged
12. Roadmap                                             unchanged (PR-6.5 will rewrite)
13. Documentation                                       unchanged
14. FAQ                                                 unchanged
15. Contributing & License                              unchanged
```

---

## Commits planned

| # | Commit | Touches | Why split |
|---|---|---|---|
| 1 | `docs(internal): PR-4 plan for Quickstart 7→3 cut` | `docs/internal/pr_plan_pr4_quickstart_3_steps.md` (this file) | Plan-doc-first per project convention (matches PR-2's `1821f50`) |
| 2 | `docs(readme): cut Quickstart from 7 sections to 3 steps` | `README.md` | The substantive change. Reviewable as a single diff. |
| 3 | `docs(readme): fix internal anchor links after Quickstart restructure` | `README.md` | Anchor audit pass — split so reviewers can diff anchor changes separately from prose |
| 4 | `docs(internal): PR-4 Round-2 reviewer fold` | varies | If 3-agent rotation surfaces fold items (anticipated convergent finding per 9-PR streak per memory) |

Anticipate 2–4 commits total. No code, no tests, no lockfile.

---

## Anchor link audit

Sections referenced from elsewhere in the README that may break after restructure:

- `#6-see-what-got-indexed` — referenced from `README.md:502` ("Covered in [Quickstart §6](#6-see-what-got-indexed)"). New anchor will be `#see-what-got-indexed` (after relocation to "After the wizard" section).
- `#observe-the-agent` — referenced from `README.md:66` and `README.md:308`. Untouched.
- `#build-your-semantic-layer` — referenced from `README.md:307`. Untouched.
- `#operate-over-time` — referenced from `README.md:309`. Untouched.
- `#how-it-fits` — referenced from `README.md:22`. Untouched.
- `#validating-sql-claude-generates` — external, points at `docs/setup.md`. Untouched.
- `#inspecting-tool-shapes-with-the-official-mcp-inspector` — external, points at `docs/setup.md`. Untouched.

**Action in commit 3:** grep all `(#...)` anchor references in README; verify each target still exists or has been redirected. Add a one-line `<!-- PR-4 -->` comment near any renamed anchor for future-archaeology.

---

## Test plan

### Automated

- CI (lint + unit + integration + security scans) — all 5 jobs green
- No coverage delta expected (README-only)
- No `ruff` / `mypy` impact

### Manual smoke (MANDATORY per `feedback_manual_smoke_mandatory.md`)

End-to-end smoke on a **completely fresh machine state**:

1. Fresh venv outside the repo: `python3 -m venv /tmp/pr4-smoke && source .../bin/activate && pip install schemabrain==0.3.0`
2. **Demo path:** `schemabrain init` → press Enter at URL prompt → press Enter at API key prompt → expect wizard to auto-`docker run` Postgres on host port 5432, load fixture, run stages 2 (Index) / 3 (skip — no key) / 4 (skip) / 5 (FK-mined joins) / 6 (Wire Claude Desktop) / 7 (Next). Total time < 60s. Result: Claude Desktop config has `schemabrain` entry.
3. **Own-DB path:** in a fresh terminal, `schemabrain init` → paste a Postgres URL when prompted → expect wizard to skip auto-Docker and connect to the supplied URL.
4. **Cross-link verification:** open the rendered README on GitHub.com (after pushing the branch) and click every `(#...)` anchor in the Quickstart + "After the wizard" + troubleshooter sections — every one must resolve.
5. **GitHub rendered preview** of README.md in the PR diff view — verify section headers, code-fence languages, and the collapsible `<details>` block render correctly.

### Reality checks

- Headline command at README:16-20 (`pip install schemabrain` + `schemabrain init`) is the same surface the new Step 1+2 promise — these must literally match
- "What each stage does" detail block matches what the wizard actually prints (re-read `schemabrain/setup/init_flow.py` if anything's been renamed since PR-2)
- `--yes` claim ("skips all interactive prompts for CI use") is true — verify against `_ui.py`'s assume_yes path

---

## Reviewer rotation

Per project convention (matches PR-2, PR-3 family, PR #79, PR #80):

| Agent | Scope |
|---|---|
| **python-reviewer** | N/A for README-only PR — SKIP or lighter-touch pass |
| **silent-failure-hunter** | N/A for docs — SKIP |
| **Reality Checker** | **MANDATORY** — every claim in the new Quickstart traced to actual wizard behavior or shipped code. Reality Checker has caught Quickstart drift before (PR #81's 5 CRITICAL findings including fabricated column counts). |
| **UX Researcher** | **MANDATORY** — new 3-step copy read by an agent that hasn't seen the prior 7-step version, asked to walk through and report friction. Likely surfaces phrasing nits the Reality Checker won't. |
| **Codebase Onboarding Engineer** | optional — could validate that "After the wizard" section relocations don't strand new users without an obvious next step |

Expectation: 2-agent rotation (Reality Checker + UX Researcher) is the minimum; 3-agent if Onboarding Engineer signal is wanted. 9 of last 9 PR families had convergent findings — assume this one will too, plan for a Round-2 fold commit.

---

## Risks

| Risk | Mitigation |
|---|---|
| Broken anchor links after restructure | Dedicated commit 3 + grep audit + manual GitHub-rendered-page click-through |
| "I don't have Docker" path becomes invisible | The auto-Docker fallback in the wizard already prints a guided block on subprocess/readiness failure (D2 design). Verify still triggers; mention in Step 2 prose. |
| Users who like the explicit URL/env approach feel patronized | Keep the existing `--url-env` / `--store-path` flag forms documented in `docs/setup.md` for the explicit-form-preferred path. Quickstart shows the minimal bare path; setup.md shows the full surface. |
| Wizard auto-Docker prompts user for Docker install if missing | Already handled in D2 (idempotent + guided fallback). Verify in manual smoke from a machine *without* Docker installed. |
| Sample-session value-prop hook above Quickstart looks misaligned with the shorter Quickstart below it | Re-read Sample session in context after restructure; minor edits permitted if hook still feels disproportionate. Not the focus of this PR. |
| `examples/anthropic_demo.py` becomes harder to find when §7 relocates | PR-6 (next) explicitly promotes anthropic_demo. PR-4 just relocates it into "After the wizard" — equivalent visibility, different ordering. PR-6 then promotes it above the new Quickstart entirely. |

---

## Hard contracts (do NOT touch in this PR)

1. **Sample session block** at `README.md:26-72` — value-prop hook before Quickstart, separate scope. Untouched.
2. **"If something went wrong" troubleshooter** at `README.md:289-299` — already exists; PR-4 may fold the `doctor` recipe in from old §4 but doesn't restructure the existing 4 troubleshooter entries.
3. **All sections below Quickstart** (Build your semantic layer / Observe the agent / Operate over time / How it fits / Roadmap / Docs / FAQ / Contributing) — untouched. PR-5 / PR-6.5 own those.
4. **Headline install snippet** at `README.md:16-22` — already correct after PR-3 publish; not re-touched.
5. **No flag deprecations.** `--url-env`, `--store-path`, `--from-dbt`, `--yes`, `--print-only`, `--no-entities`, `--no-metrics`, `--no-joins`, `--skip-llm-confirm`, `--host` all stay valid; the Quickstart just doesn't lead with them.
6. **No CHANGELOG update.** README-only documentation change does not warrant a CHANGELOG entry; the post-publish reality is what the README is catching up to. (Override only if Reality Checker flags otherwise.)

---

## Out of scope (deliberately)

- **Firewall property promotion** — `--pii-block`, audit log, validated-SQL visibility lift. That's PR-5.
- **`anthropic_demo.py` promotion** to above-the-Quickstart. That's PR-6.
- **README length cut to ~250 lines.** Quickstart will shrink, but the README will still be ~600 lines after PR-4. The full length cut + Roadmap rewrite + honest-disclaimer sharpening is PR-6.5.
- **Visual hero (logo / OG banner / demo.gif embed).** PR-7.
- **Wizard code changes.** Behavioral surface is already in the right shape — PR-4 only updates the docs that describe it.

---

## Definition of done

- [ ] Quickstart section renders as exactly 3 numbered sub-sections
- [ ] All section anchors referenced from elsewhere in the README still resolve
- [ ] Manual smoke (demo path + own-DB path) PASSES on a fresh machine
- [ ] Reality Checker traces every Quickstart claim — 0 unsubstantiated
- [ ] UX Researcher walk-through reports no blocking friction
- [ ] CI: all 5 jobs green
- [ ] PR description includes the section-migration table + before/after line count + manual smoke evidence

---

## Open questions

1. **Keep `schemabrain --version` confirmation in Step 1?** Pro: validates install. Con: extra command users skip. **Lean: keep**, it's one extra line and confirms the version expectation (`0.3.0`).
2. **Should the `<details>` collapsible "What each stage does" inside Step 2 be removed entirely (move to docs/setup.md)?** Pro: keeps Step 2 lean. Con: collapsibles are useful for users who *do* want detail without scrolling away. **Lean: keep as `<details>`** — collapsed by default.
3. **Should the Step 3 "Cmd+Q" caveat be a sub-bullet or an inline emphasis?** Existing §5 has it as a step-1-of-3 inside the section. **Lean: keep as 3-bullet sub-list inside Step 3** — visible enough.
4. **Move `doctor` recipe into the troubleshooter, or keep as a separate "Verify the install" section?** **Lean: fold into troubleshooter** — wizard's stage 6 already verifies wiring on the happy path.

These are user/reviewer discretion calls; surface for sign-off before commit 2.

---

## Predecessor / successor links

- **Depends on:** PR-3 (install honesty) — must be merged so headline install actually works
- **Memory:** see POSITIONING SHIFT entry in `MEMORY.md` line 5 + the `project_positioning_firewall.md` detail
- **Audit source:** `docs/internal/positioning_audit_2026_05_20.md` (the audit that surfaced the 7→3 gap)
- **Successor:** PR-5 (firewall property promotion above the fold)
