"""Tests for `schemabrain.setup.checks` — the health-check data model.

Goals:

  1. `Check` is a frozen DTO with a closed-grammar `outcome` Literal,
     validated in `__post_init__` like the other core dataclasses.
  2. `CheckResult` aggregates checks into a stable shape: counts that
     match the underlying check list, an exit code that is `1` iff any
     check failed (warnings never break CI), and a JSON dict shape
     that matches the contract surfaced via `schemabrain doctor --json`.
  3. `run_checks` runs each callable in order, never aborts on a single
     check's exception — a raising check becomes a `fail` Check whose
     name is the callable's `__name__` and whose message names the
     exception type. A buggy check must not crash `doctor`.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from schemabrain.setup.checks import (
    Check,
    CheckOutcome,
    CheckResult,
    CheckSummary,
    render_json,
    run_checks,
)


class TestCheck:
    def test_pass_check_with_no_suggested_next(self) -> None:
        c = Check(name="uvx_invocable", outcome="pass", message="uvx 0.5.1 on PATH")
        assert c.name == "uvx_invocable"
        assert c.outcome == "pass"
        assert c.suggested_next is None

    def test_fail_check_with_suggested_next(self) -> None:
        c = Check(
            name="host_config_present",
            outcome="fail",
            message="no schemabrain entry in claude_desktop_config.json",
            suggested_next="run `schemabrain init`",
        )
        assert c.outcome == "fail"
        assert c.suggested_next == "run `schemabrain init`"

    def test_warn_outcome_allowed(self) -> None:
        c = Check(
            name="version_pin_drift",
            outcome="warn",
            message="snippet pins 0.2.0a1, installed 0.3.0",
        )
        assert c.outcome == "warn"

    def test_frozen_dataclass_cannot_be_mutated(self) -> None:
        # Mirrors the pattern of every other core dataclass — Check is
        # an immutable DTO. Future doctor code must construct a new
        # Check, never mutate one in flight.
        c = Check(name="x", outcome="pass", message="ok")
        with pytest.raises(FrozenInstanceError):
            c.outcome = "fail"  # type: ignore[misc]

    def test_invalid_outcome_rejected_at_construction(self) -> None:
        # Literal is a type hint, not a runtime guard, so we enforce
        # the closed grammar in __post_init__ the same way Metric/Entity do.
        with pytest.raises(ValueError, match="outcome"):
            Check(name="x", outcome="passed", message="ok")  # type: ignore[arg-type]

    def test_empty_name_rejected(self) -> None:
        # The name is a stable identifier users grep CI logs for.
        # An empty name is always a programming error.
        with pytest.raises(ValueError, match="name"):
            Check(name="", outcome="pass", message="ok")

    def test_empty_message_rejected(self) -> None:
        # A check without a human-readable message is useless in the
        # rendered output. Catch this at construction.
        with pytest.raises(ValueError, match="message"):
            Check(name="x", outcome="pass", message="")

    def test_check_outcome_literal_has_three_values(self) -> None:
        # Lock the closed grammar at the type level. Adding a fourth
        # outcome (e.g. "skip") is a semantic change that should force
        # this test to be updated deliberately.
        assert set(get_args(CheckOutcome)) == {"pass", "warn", "fail"}


class TestCheckSummary:
    def test_zero_counts_for_empty(self) -> None:
        s = CheckSummary(passed=0, warned=0, failed=0)
        assert s.passed == 0
        assert s.warned == 0
        assert s.failed == 0

    def test_holds_mixed_counts(self) -> None:
        s = CheckSummary(passed=3, warned=2, failed=1)
        assert (s.passed, s.warned, s.failed) == (3, 2, 1)

    def test_frozen(self) -> None:
        s = CheckSummary(passed=1, warned=0, failed=0)
        with pytest.raises(FrozenInstanceError):
            s.passed = 5  # type: ignore[misc]


class TestCheckResult:
    def test_empty_result_exit_code_is_zero(self) -> None:
        # `doctor` with no checks at all is a degenerate but legal
        # state (e.g. all checks gated off by flags). Returns 0.
        r = CheckResult(checks=())
        assert r.exit_code == 0
        assert r.summary == CheckSummary(passed=0, warned=0, failed=0)

    def test_all_pass_exit_code_is_zero(self) -> None:
        r = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="pass", message="ok"),
            )
        )
        assert r.exit_code == 0
        assert r.summary == CheckSummary(passed=2, warned=0, failed=0)

    def test_one_fail_exit_code_is_one(self) -> None:
        r = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="fail", message="broken", suggested_next="fix it"),
            )
        )
        assert r.exit_code == 1
        assert r.summary == CheckSummary(passed=1, warned=0, failed=1)

    def test_only_warns_exit_code_is_zero(self) -> None:
        # Warnings are observation, not failure. CI pipelines using
        # `doctor` as a health check must not flake on stale-version
        # warns or missing-index warns.
        r = CheckResult(
            checks=(
                Check(name="a", outcome="warn", message="stale"),
                Check(name="b", outcome="warn", message="empty"),
            )
        )
        assert r.exit_code == 0
        assert r.summary == CheckSummary(passed=0, warned=2, failed=0)

    def test_summary_counts_each_outcome(self) -> None:
        r = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="m"),
                Check(name="b", outcome="pass", message="m"),
                Check(name="c", outcome="warn", message="m"),
                Check(name="d", outcome="fail", message="m"),
            )
        )
        assert r.summary == CheckSummary(passed=2, warned=1, failed=1)

    def test_to_json_dict_shape_matches_contract(self) -> None:
        # The shape is part of the user-facing contract surfaced as
        # `schemabrain doctor --json`. Keys: "checks", "summary",
        # "exit_code". `summary` uses "pass"/"warn"/"fail" (matching
        # the outcome strings) rather than the Python "passed"/etc
        # attribute names.
        r = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="fail", message="bad", suggested_next="try x"),
            )
        )
        d = r.to_json_dict()
        assert set(d.keys()) == {"checks", "summary", "exit_code"}
        assert d["exit_code"] == 1
        assert d["summary"] == {"pass": 1, "warn": 0, "fail": 1}
        assert d["checks"] == [
            {"name": "a", "outcome": "pass", "message": "ok", "suggested_next": None},
            {
                "name": "b",
                "outcome": "fail",
                "message": "bad",
                "suggested_next": "try x",
            },
        ]

    def test_to_json_dict_preserves_check_order(self) -> None:
        # Ordering is part of the contract — a user reading the output
        # expects checks in the order doctor ran them, not alphabetised.
        names = [f"check_{i}" for i in range(10)]
        r = CheckResult(checks=tuple(Check(name=n, outcome="pass", message="ok") for n in names))
        d = r.to_json_dict()
        assert [c["name"] for c in d["checks"]] == names

    def test_to_json_dict_roundtrips_via_json_module(self) -> None:
        # The dict must be JSON-serialisable with stdlib `json` — no
        # exotic objects, no datetimes, no enums.
        r = CheckResult(
            checks=(Check(name="a", outcome="warn", message="ok", suggested_next=None),)
        )
        encoded = json.dumps(r.to_json_dict())
        decoded = json.loads(encoded)
        assert decoded["summary"] == {"pass": 0, "warn": 1, "fail": 0}

    def test_check_result_is_frozen(self) -> None:
        r = CheckResult(checks=())
        with pytest.raises(FrozenInstanceError):
            r.checks = (Check(name="x", outcome="pass", message="ok"),)  # type: ignore[misc]


class TestRunChecks:
    def test_runs_each_check_in_order(self) -> None:
        # Order matters for the rendered output and for doctor's
        # contract — checks run in the sequence the caller supplied.
        called: list[str] = []

        def check_a() -> Check:
            called.append("a")
            return Check(name="a", outcome="pass", message="ok")

        def check_b() -> Check:
            called.append("b")
            return Check(name="b", outcome="pass", message="ok")

        run_checks([check_a, check_b])
        assert called == ["a", "b"]

    def test_aggregates_results_in_call_order(self) -> None:
        def check_a() -> Check:
            return Check(name="a", outcome="pass", message="ok")

        def check_b() -> Check:
            return Check(name="b", outcome="fail", message="bad")

        result = run_checks([check_a, check_b])
        assert [c.name for c in result.checks] == ["a", "b"]
        assert result.exit_code == 1

    def test_empty_check_list_yields_empty_result(self) -> None:
        result = run_checks([])
        assert result.checks == ()
        assert result.exit_code == 0
        assert result.summary == CheckSummary(passed=0, warned=0, failed=0)

    def test_raising_check_becomes_fail_outcome(self) -> None:
        # A buggy check must not crash `doctor` — the user gets a
        # visible fail row instead, with the exception preserved in
        # the message for triage.
        def broken_check() -> Check:
            raise RuntimeError("kaboom")

        result = run_checks([broken_check])
        assert len(result.checks) == 1
        assert result.checks[0].outcome == "fail"
        assert "RuntimeError" in result.checks[0].message
        assert "kaboom" in result.checks[0].message
        assert result.exit_code == 1

    def test_raising_check_preserves_function_name(self) -> None:
        # The synthesised fail Check uses the callable's __name__ so
        # the user can still identify which check failed.
        def specifically_named_check() -> Check:
            raise ValueError("nope")

        result = run_checks([specifically_named_check])
        assert result.checks[0].name == "specifically_named_check"

    def test_raising_check_in_middle_does_not_abort_others(self) -> None:
        # Sequential runner must reach every check, even if an earlier
        # one raised. The runner's guarantee: one bad check never
        # hides the others.
        order: list[str] = []

        def first() -> Check:
            order.append("first")
            return Check(name="first", outcome="pass", message="ok")

        def middle_raises() -> Check:
            order.append("middle")
            raise OSError("disk full")

        def last() -> Check:
            order.append("last")
            return Check(name="last", outcome="warn", message="watch this")

        result = run_checks([first, middle_raises, last])
        assert order == ["first", "middle", "last"]
        assert [c.name for c in result.checks] == ["first", "middle_raises", "last"]
        assert [c.outcome for c in result.checks] == ["pass", "fail", "warn"]
        assert result.exit_code == 1

    def test_raising_check_includes_suggested_next(self) -> None:
        # A check-internal bug is not the user's problem — the
        # suggested_next points them at the project issue tracker
        # rather than leaving them to debug schemabrain itself.
        def broken_check() -> Check:
            raise RuntimeError("oh no")

        result = run_checks([broken_check])
        assert result.checks[0].suggested_next is not None
        assert "issue" in result.checks[0].suggested_next.lower()

    def test_render_json_produces_indented_trailing_newline(self) -> None:
        # `doctor --json` writes this string straight to stdout; the
        # trailing newline keeps shells happy when piping to a file.
        r = CheckResult(checks=(Check(name="a", outcome="pass", message="ok"),))
        out = render_json(r)
        assert out.endswith("\n")
        assert '\n  "checks"' in out  # 2-space indented

    def test_render_json_roundtrips_to_equivalent_dict(self) -> None:
        r = CheckResult(
            checks=(
                Check(name="a", outcome="pass", message="ok"),
                Check(name="b", outcome="warn", message="watch"),
            )
        )
        assert json.loads(render_json(r)) == r.to_json_dict()

    def test_lambda_without_name_falls_back_to_safe_default(self) -> None:
        # Lambdas have __name__ == "<lambda>". The runner falls back
        # to a placeholder name rather than emitting that as a user-
        # visible identifier.
        def _raise() -> Check:
            raise RuntimeError("boom")

        # Strip __name__ to simulate a hand-rolled callable without one.
        anonymised = _raise
        # `delattr` on a function's __name__ isn't allowed; instead set
        # it to empty to exercise the fallback branch.
        anonymised.__name__ = ""
        result = run_checks([anonymised])
        assert result.checks[0].name != ""
        assert result.checks[0].outcome == "fail"
