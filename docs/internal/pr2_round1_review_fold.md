# PR-2 Round-1 reviewer-rotation fold plan

**Branch:** `feat/post-pr79-polish-bundle`
**Review date:** 2026-05-20
**Commits reviewed:** `ee25c82` (F5), `e31ea3e` (F1), `f887469` (F3), `8873adb` (F4)
**Reviewers:** python-reviewer, silent-failure-hunter, Reality Checker

## Verdict

- **Reality Checker:** SHIP — all 19 PR-2 claims verified PASS (with one narrow caveat on F5 auth-guard claim scope).
- **python-reviewer + silent-failure-hunter:** Found 1 convergent HIGH + 1 CRITICAL + 2 non-convergent HIGH + 3 MEDIUM + 2 LOW. Must-fix the CRITICAL + HIGHs before continuing PR-2.

## Findings (10 total)

### CRITICAL

**C1. Abort panel fires before cancelled-by-user exit-0 check** (silent-failure-hunter)
- File: `schemabrain/cli.py:5097-5113`
- Bug: `_render_wizard_after(result, ...)` runs UNCONDITIONALLY at line 5097, BEFORE the `cancelled by user` branch at line 5110. User who declines the F3 overwrite prompt sees a red "Stopped at stage 6 of 7" failure panel + "cancelled — no changes made." + exit 0. Misleading UX for a deliberate cancellation.
- Fix: Move the `aborted_at.message.startswith("cancelled by user")` check BEFORE `_render_wizard_after`. Print "cancelled — no changes made." and return 0 without rendering the abort panel.

### HIGH (convergent — both python-reviewer + silent-failure-hunter)

**H1. `_get_overwrite_comparison` re-peek dead code**
- File: `schemabrain/setup/wizard.py:2473`
- Bug: After `init()` writes the new entry, the post-write re-peek sees the file with the new entry → `compare_existing_claude_desktop_entry` returns `state="unchanged"`, never `"differs_store_path_only"`. The `(replaced /old → /new)` trailer in `_wire_host_message` is **dead code in production**. Tests don't catch it because they mock `peek_claude_desktop_overwrite` to return the same object on both calls.
- Fix: Thread the pre-check `comparison` from `_stage_wire_host` through a local variable to the `_wire_host_message` call. Delete `_get_overwrite_comparison` entirely.

### HIGH (non-convergent)

**H2. `_is_overloaded_error(anthropic_module: object)` — `object`-typed param with `getattr`** (python-reviewer)
- File: `schemabrain/errors_render.py:518`
- Bug: Same anti-pattern as `_compose_footer_line(summary: object)` (PR #4) and `_compose_entity_brand_line(entity: object)` (PR #7) — both prior convergent HIGH findings. Param typed `object` but body does `getattr(anthropic_module, "OverloadedError", None)`. The `getattr` is correct for SDK version tolerance; the typing should reflect reality.
- Fix: `import types`; change signature to `anthropic_module: types.ModuleType`.

**H3. "cancelled by user" message-prefix string coupling across module boundary** (silent-failure-hunter)
- Files: `schemabrain/cli.py:5110` (consumer) + `schemabrain/setup/wizard.py:2451` (producer)
- Bug: Two separate string literals, no shared constant, no compile-time guard. A copy-edit to the wizard message silently breaks the exit-code contract (declined user gets exit 2 instead of exit 0).
- Fix: Add `user_cancelled: bool = False` field to `StageOutcome`. Set it to `True` in the F3 cancellation branch. Replace the message-prefix check in `_cmd_init` with `aborted_at.user_cancelled`. ~80 LOC across wizard + cli + tests.

### MEDIUM (all python-reviewer)

**M1. `ClaudeDesktopEntryComparison` imported twice**
- File: `schemabrain/setup/wizard.py:70,83`
- Bug: Live import at line 70 (correct — used at runtime). Duplicate import under `TYPE_CHECKING` at line 83 (no-op, misleading).
- Fix: Remove line 83.

**M2. `make_console` binding inconsistency**
- File: `schemabrain/setup/wizard.py:65,2431`
- Bug: `_ui.stderr_is_interactive_tty()` accessed through module (so tests can monkeypatch `_ui.stderr_is_interactive_tty`). But `make_console` accessed via top-level import → tests that want to patch the wizard's `make_console` need a separate `wizard.make_console` patch. Latent asymmetry.
- Fix: Either use `_ui.make_console(stderr=True)` for consistency, or add a comment noting the intentional inconsistency. Recommend consistency.

**M3. `_try_render_llm_failure` uses `getattr(exc, "message", None)` on `BaseException`**
- File: `schemabrain/cli.py:6890`
- Bug: Same `object`-access-via-getattr pattern as H2. Lower severity because the `or str(exc)` fallback is complete and `classify_llm_failure` has already confirmed `exc` is an Anthropic SDK type.
- Fix: After `kind = classify_llm_failure(exc)` returns non-None, cast or expose a `cause_from_llm_error(exc) -> str` helper in `errors_render.py` next to `classify_llm_failure`.

### LOW

**L1. `_llm_failure_next_step` doesn't raise on unknown kind** (python-reviewer)
- File: `schemabrain/setup/wizard.py:1248-1270`
- Bug: Three separate `if kind == ...` dispatch functions across two modules. `_llm_failure_titles` + `_llm_failure_retry_hint` in `errors_render.py` raise `ValueError` on unknown kinds; `_llm_failure_next_step` in `wizard.py` silently falls through to `None` branch.
- Fix: Add `raise ValueError(f"unknown kind {kind!r}")` at the end of the known-kind dispatch (before the `kind is None` branch).

**L2. `classify_llm_failure` swallows ImportError** (silent-failure-hunter)
- File: `schemabrain/errors_render.py:495-498`
- Bug: `try: import anthropic except ImportError: return None`. Hard-dep contract makes this unreachable today, but if the dep is ever relaxed, the silent fallback becomes a HIGH-level regression with no warning.
- Defer: No change required given current hard-dep contract.

## Caveat (Reality Checker)

**F5 auth-guard claim narrowness**
- File: `schemabrain/cli.py:1987`
- Claim was: "AuthenticationError keeps its existing GuidedError path — F5 doesn't shadow it." This is TRUE for `_cmd_index` (auth guard fires BEFORE the F5 call). It is FALSE for `_cmd_entities_suggest` and `_cmd_metrics_suggest` — those commands had no pre-existing auth guard, so an `AuthenticationError` (an `APIStatusError` subclass) now classifies as `"api_error"` and renders Shape C with a less-tailored hint.
- Net effect: strictly better than pre-F5 (which was a raw traceback). Worth one-line note in PR body but no code change needed.

## Fold plan (in this order)

1. **C1** — reorder `_render_wizard_after` after cancellation check (`cli.py`)
2. **H1** — thread `comparison` through local var, delete `_get_overwrite_comparison` (`wizard.py`)
3. **H2** — type `anthropic_module: types.ModuleType` (`errors_render.py`)
4. **H3** — add `user_cancelled: bool` to `StageOutcome`, set in wizard, check in cli (`wizard.py` + `cli.py` + tests)
5. **M1** — remove duplicate `TYPE_CHECKING` import (`wizard.py`)
6. **M2** — `_ui.make_console(stderr=True)` for consistency (`wizard.py`)
7. **M3** — narrow type or extract `cause_from_llm_error` helper (`cli.py` + `errors_render.py`)
8. **L1** — raise on unknown kind in `_llm_failure_next_step` (`wizard.py`)
9. L2 — DEFER (hard-dep contract makes it unreachable)

## Convergent-finding count

This is the **8th consecutive PR/PR-family** with a convergent finding (`_get_overwrite_comparison` re-peek caught by both python-reviewer + silent-failure-hunter). The 3-agent reviewer rotation continues to earn its keep.

## Pre-fold metrics

- 4162 tests passing
- 99.05% coverage
- wizard.py at 100% (will be re-checked post-fold)
- ruff + format clean
