# PR-2 plan — post-PR-#79 polish (full backlog bundle)

**Status:** PROPOSED · awaiting implementation kickoff
**Branch name:** `feat/post-pr79-polish-bundle`
**Branches off:** `main @ d55f668` (PR #79 merge commit)
**Anticipated commit count:** 8 commits · ~1 week effort

---

## Motivation

PR #79 (day-one UX overhaul) shipped 2026-05-20 and surfaced 4 net-new findings during live smoke. Combined with originally-deferred PR-2 backlog items, this PR bundles the full "wizard error/state surfaces + sub-step UX continuity" theme into one coherent ship.

**Why bundle, not split**: the new findings (F1, F3, F4, F5) plus the deferred items (Rich Live timer, auto-docker, host-config backup, creds persistence) all touch the same wizard / suggest-command surface area. Splitting risks merge-conflict churn and forces reviewers to re-load the same context 3-4 times. One coherent PR with clearly-segregated commits is reviewable in a single session.

**Why now**: F5 is HIGH severity — every new user on the README Quickstart path hits a Python traceback during any Anthropic outage. We saw this live in PR #79 §5 testing. Real users will hit it on day one.

---

## Findings inventory

### From PR #79 live smoke (2026-05-20)

| # | Severity | Surface | Source | Description |
|---|---|---|---|---|
| **F5** | **HIGH** | `index --enrich`, `entities suggest`, `metrics suggest`, wizard stages 3+4 | Deferred Shape C from design-system arc | Anthropic `OverloadedError` (529), `RateLimitError` (429), `APIConnectionError`, generic `APIError` bubble up as raw 50-line Python tracebacks. No retry hint, no `--no-enrich` fallback, no graceful skip path. |
| **F4** | LOW-MED | wizard stage 2 | Pre-existing logic | Stage 2 renders `⊘ already indexed: 7 tables present` even when stage 1 freshly wrote those tables in the same run. Misleading "already" framing for the new-user path. |
| **F3** | MED | wizard stage 6 | Pre-existing logic exposed more by PR-1 | Host-overwrite prompt fires AFTER hero panel but BEFORE 7-stage table renders. Visually orphaned from stage 6. Stage 6 ends up rendering as already-complete in the final table, prompt has no visible owner. |
| **F1** | MED | `entities/metrics/joins suggest` (standalone commands) | Wizard scope-out in PR-1 | Standalone-suggest commands show no spinner/progress during ~20s LLM call. Wizard variant ships cost-preview (PR-1 c6c899e); standalone variants don't. Asymmetry between wizard and CLI surfaces. |

### From PR-1 originally-deferred backlog

| # | Severity | Description |
|---|---|---|
| **D1** | MED | Full Rich Live cost-preview with elapsed timer for wizard sub-steps (PR-1 shipped conservative static-line variant; the elapsed-timer version needs `_wizard_stage_context` refactor) |
| **D2** | LOW-MED | Auto-`docker compose up` from stage 0 (eliminates the user-runs-two-commands-in-another-terminal friction in the demo path). Wizard contract change — needs try/except pattern decision. |
| **D3** | LOW-MED | Auto-write Claude Desktop config with `.bak` backup + diff preview (currently overwrites silently after the confirm prompt; would let users see exactly what's changing) |
| **D4** | LOW | Persist `ANTHROPIC_API_KEY` to `.env` with explicit consent (currently user must re-export between sessions; one-line prompt + opt-in write) |
| **D5** | LOW | Auto-load fixture via SQLAlchemy in demo path (eliminates the second `docker run` for psql fixture load) |

---

## Commit order (8 commits)

### Commit 1 — F5: render LLM failure shape across all 5 callsites

**Files touched:**
- `schemabrain/errors_render.py` (extend with `render_llm_failure(error_kind, ...)` — Shape C from design-system migration arc handoff)
- `schemabrain/cli.py` (5 try/except blocks in `_cmd_index`, `_cmd_entities_suggest`, `_cmd_metrics_suggest`)
- `schemabrain/setup/wizard.py` (2 try/except blocks in `_run_entity_suggestion`, `_run_metric_suggestion`)
- `tests/test_errors_render.py` (new test class `TestRenderLlmFailure` × 4 error kinds)
- `tests/test_cli.py` (extend with `TestCmdIndexLlmFailure`, `TestCmdEntitiesSuggestLlmFailure`, `TestCmdMetricsSuggestLlmFailure`)
- `tests/test_wizard.py` (extend with stage-3 + stage-4 LLM failure outcome assertions)

**Caught exceptions:**
- `anthropic.OverloadedError` (529) — render with hint: "Anthropic is overloaded — retry in 30s, or run with `--no-enrich` to skip"
- `anthropic.RateLimitError` (429) — render with hint: "rate-limited — wait and retry; consider lowering `SCHEMABRAIN_*_CONCURRENCY`"
- `anthropic.APIConnectionError` — render with hint: "couldn't reach Anthropic — check network / proxy"
- `anthropic.APIError` (catch-all) — render with the response message + hint to retry

**Wizard integration**: stage 3 + stage 4 must NOT raise (wizard stage contract). Instead, return `StageOutcome(status="failed", next_step="run `schemabrain entities suggest --apply` later when Anthropic recovers")`.

**Standalone CLI integration**: catch + render + exit 2 (operational failure).

**Effort:** ~3 hours. The most load-bearing commit in the PR — F5 is the headline.

---

### Commit 2 — F1: standalone-suggest cost preview + spinner

**Files touched:**
- `schemabrain/cli.py` (`_cmd_entities_suggest`, `_cmd_metrics_suggest`, `_cmd_joins_suggest`)
- `tests/test_cli.py` (extend with cost-preview line assertions for all 3 standalone commands)

**Shape:** Reuse `print_llm_stage_preamble` from `_ui.py` (already exists, used in wizard). Add a `Rich.live.Live` spinner during the actual LLM call.

- `entities suggest`: ~$0.01 cap $1.00 sonnet preamble, then spinner "Asking Claude to identify business entities..."
- `metrics suggest`: ~$0.02 cap $0.50 sonnet preamble, then spinner "Asking Claude to define metrics..."
- `joins suggest`: deterministic (FK + query-log), no cost, just spinner "Mining canonical joins..."

**Critical:** `--quiet` mode (used by CI / scripts) must skip both preamble + spinner. Test must cover both interactive + `--quiet` paths.

**Effort:** ~1.5 hours.

---

### Commit 3 — F3: host-overwrite prompt integrated into stage 6

**Files touched:**
- `schemabrain/setup/wizard.py` (`_run_host_wire` stage — accept the prompt result via the existing `pause_active_spinner` pattern)
- `schemabrain/cli.py` (`_cmd_init` — remove upfront overwrite check; defer to stage 6)
- `schemabrain/setup/hosts.py` (auto-accept when only differing field is `store_path` — emit "wrote (replaced /old → /new)" in stage 6 message)
- `tests/test_cli_init.py` (extend with `TestStageSixOverwriteIntegration`)
- `tests/test_wizard.py` (extend stage 6 cases)

**Decision matrix for auto-accept vs prompt:**
- Same config (identical fields) → "no changes" (current behavior, unchanged)
- Only `store_path` differs → auto-accept + render `wrote (replaced /old → /new)`
- Any other field differs (host name, env var, version pin) → prompt with diff preview ("the following fields differ: ... overwrite?")

**Effort:** ~2 hours. The most architecturally complex commit — touches stage handler contract.

---

### Commit 4 — F4: stage 2 distinguishes same-run vs prior-run writes

**Investigation required first** (~30 min): trace stage 1 → stage 2 in `schemabrain/setup/wizard.py` to confirm root cause. Hypothesis: stage 1 writes connection + tables, stage 2 sees what stage 1 wrote and skips. Need wizard run-context to track "did I write these in THIS run?"

**Files touched (after investigation confirms):**
- `schemabrain/setup/wizard.py` (`_run_index_stage` — read wizard run-context; if same-run write, emit `7 tables · 30 columns indexed (just now)` instead of `already indexed`)
- `tests/test_wizard.py` (extend with `TestIndexStageSameRunVsPriorRun`)

**Effort:** ~1.5 hours including investigation.

---

### Commit 5 — D1: full Rich Live cost-preview with elapsed timer

**Files touched:**
- `schemabrain/setup/wizard.py` (`_wizard_stage_context` refactor — needs to accept Live-mode renderer for sub-step UI)
- `schemabrain/_ui.py` (extend `print_llm_stage_preamble` to a `live_llm_stage_progress(*, cost_est, cap, model)` context manager that updates elapsed time)
- `tests/test_ui_primitives.py` (extend `TestLiveLlmStageProgress`)
- `tests/test_wizard.py` (extend stages 3 + 4 to assert elapsed-timer rendering)

**Trade-off**: this is the conservative "upgrade the wizard sub-step shape from static line to live elapsed timer" work deferred from PR-1. It's MEDIUM risk because it touches `_wizard_stage_context` which most stages use.

**Effort:** ~2 hours.

---

### Commit 6 — D2: auto-`docker compose up` from stage 0

**Files touched:**
- `schemabrain/setup/setup_stage.py` (`_handle_demo_path` — execute the two `docker run` commands programmatically; tail container logs until Postgres is ready)
- `schemabrain/setup/setup_stage.py` (add `_wait_for_postgres_ready(url, timeout=30)` helper using a connect-loop)
- `tests/test_setup_stage.py` (extend with `TestAutoDockerRun`)

**Risk**: Docker can fail for many reasons (no Docker, image pull failure, port conflict, permission denied). Each branch needs a clean fallback to the existing copy-paste UX with explicit "couldn't auto-run, here are the commands to run manually" guidance.

**Effort:** ~2.5 hours. Pattern decision (try/except boundaries) is the biggest variable.

---

### Commit 7 — D3: Claude Desktop config `.bak` backup + diff preview

**Files touched:**
- `schemabrain/setup/hosts.py` (`apply_to_claude_desktop` — write `.bak` before overwrite; render diff via `difflib.unified_diff`)
- `tests/test_hosts.py` (extend with `TestConfigBackupAndDiff`)

**Effort:** ~1.5 hours.

---

### Commit 8 — D4: persist `ANTHROPIC_API_KEY` to `.env` with consent

**Files touched:**
- `schemabrain/_ui.py` (extend `prompt_for_anthropic_key` to offer `[y/N] save to .env for next time?` after a successful key is provided)
- `schemabrain/cli.py` (load `.env` at startup via `python-dotenv` — already a dep)
- `tests/test_ui_primitives.py` (extend with `TestPromptForKeyEnvPersistence`)

**Critical**: `.env` write MUST be opt-in (default no). Never write the key silently. Warn if `.env` is not in `.gitignore`.

**Effort:** ~1 hour.

---

## Test + coverage strategy

- **Coverage gate stays at 99%.** Each commit ships with tests that cover its new branches. No commit ships net-negative coverage.
- **Live smoke recipe** at `docs/internal/manual_test_recipe_pr2.md` (new). Walks: F5 forced-overload test (mock Anthropic 529), F1 spinner verification, F3 overwrite-prompt path variations, F4 fresh-store stage 2 messaging.
- **Anthropic-outage simulation**: extend `tests/test_cli.py` and `tests/test_wizard.py` with monkeypatched `OverloadedError` raises at the SDK boundary to verify graceful degradation without depending on a real outage.

---

## Reviewer rotation plan

Same 3-agent pattern as PR-1 and the design-system migration arc:
- **python-reviewer** — idiomatic patterns, type safety, error-handling shape
- **silent-failure-hunter** — convergent-finding check (any error swallowed? any except too broad?)
- **Reality Checker** — independent trace of each PR claim against the code

**Reviewer rounds:**
- **Round 1**: after commit 5 (mid-PR) — catch architectural drift before commits 6-8
- **Round 2**: after commit 8 (pre-push) — final fold pass before opening PR

**Expected convergent finding count**: 1-2 (sustaining the multi-reviewer rotation discipline value; if zero, that's signal the rotation needs harder challenges).

---

## Risk + mitigations

| Risk | Mitigation |
|---|---|
| Commit 6 (auto-docker) breaks the demo path on a Docker-misconfigured machine | Every Docker failure path falls back to the existing copy-paste UX; never replaces the safe path, only augments it |
| Commit 3 (F3 prompt repositioning) breaks an existing test that asserts the prompt fires at a specific point | Audit existing host-overwrite tests in Round 1; update assertions to match the new contract |
| F5 (commit 1) wraps an exception type that exists only in newer anthropic-sdk versions | Pin the SDK minor version, OR catch `Exception` with `isinstance(e, ...)` checks that gracefully no-op when the import fails |
| Coverage gate failure post-push (same as PR #79 — would be the 2nd consecutive PR) | Run `pytest --cov-fail-under=99` locally BEFORE every push. Document in CLAUDE.md as a checklist item if it happens again. |

---

## Out of scope (PR-3 backlog)

- `D5` (auto-load fixture via SQLAlchemy in demo path) — explicitly held for PR-3 because it requires choosing a fixture-loading abstraction that we don't want to bikeshed in PR-2
- BIRD-bench evaluation harness (from benchmark roadmap memory)
- 7 LOW-severity reviewer findings from PR-1 Round-2 (already deferred from PR-1)
- Mobile app builder integration (not relevant to this surface area)

---

## Acceptance criteria

- [ ] All 8 commits land with their own tests + green CI
- [ ] Coverage stays ≥99%
- [ ] `ruff check` + `ruff format --check` clean
- [ ] Live smoke recipe walked end-to-end (`docs/internal/manual_test_recipe_pr2.md`)
- [ ] F5 hand-verified by forcing Anthropic 529 via monkeypatch (no traceback, friendly hint, exit 2)
- [ ] F1 hand-verified by running each standalone suggest command (spinner visible, cost-preview line present)
- [ ] F3 hand-verified by running `init` against an existing config with `--store-path` mismatch (auto-accept + "replaced" message)
- [ ] F4 hand-verified by running `init` against fresh store (stage 2 says "(just now)" not "already indexed")
- [ ] 3-agent reviewer rotation completed both rounds; all CRITICAL/HIGH/MEDIUM items folded; convergent findings counted
- [ ] PR body documents reviewer-rotation findings + fold commits + live-smoke evidence
- [ ] Manual test recipe at `docs/internal/manual_test_recipe_pr2.md` updated post-fold

---

## Manual smoke recipe pointer

`docs/internal/manual_test_recipe_pr2.md` — to be written as part of commit 1. Should follow the same 8-section shape as PR-1's recipe but with:
- §0 clean slate (unchanged from PR-1 — same Postgres + Claude config wipe)
- §1 fresh install (unchanged)
- §2 demo path — verify auto-docker fires (commit 6 acceptance)
- §3 wizard with API key — verify Rich Live elapsed timer in stages 3+4 (commit 5)
- §4 force Anthropic 529 — verify F5 friendly rendering, no traceback
- §5 standalone suggest commands — verify F1 spinners
- §6 init twice against same store with different `--store-path` — verify F3 auto-accept
- §7 inspect + doctor — regression check (PR-1 surfaces unchanged)

---

## Resume protocol

After this plan is approved and PR-2 starts:

1. Create branch: `git checkout -b feat/post-pr79-polish-bundle`
2. Commit 1 first (F5 renderer + 5 callsites — largest single commit, ship it early so reviewers can validate the renderer shape before everything else builds on it)
3. Run `uv run pytest --cov-fail-under=99` after EVERY commit (PR-1 ship retro lesson — would have caught the coverage drop pre-push)
4. Pause for Round-1 reviewer rotation after commit 5; fold findings before commits 6-8
5. After commit 8, Round-2 reviewer rotation; final fold
6. Walk the live smoke recipe end-to-end
7. Push + open PR + paste reviewer-rotation summary in PR body
