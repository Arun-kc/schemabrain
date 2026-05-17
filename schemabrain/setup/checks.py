"""Health-check data model + sequential runner used by `schemabrain doctor`.

A `Check` is a small frozen DTO that names a single health condition,
records its outcome (`pass` / `warn` / `fail`), carries a human-readable
message, and may carry a suggested next step. The runner takes a list
of callables, invokes each in order, and collects results into a
`CheckResult` that exposes a deterministic JSON shape and an exit code.

Two contracts:

1. **`run_checks` never lets a single check crash the run.** If a
   check raises, the runner converts the exception into a `fail` Check
   that preserves the callable's `__name__`, names the exception type,
   and points the user at the issue tracker. A buggy check is visible
   in the output, not a process-killing surprise.

2. **Warnings never affect the exit code.** Only `fail` outcomes set
   exit code to `1`. CI pipelines using `doctor` as a health probe
   must not flake on stale-version or empty-store warnings; users who
   want to gate on warnings can grep the JSON output instead.

The runner is deliberately sequential. Eleven I/O-bound checks complete
in well under a second in practice; the complexity of a parallel
executor (cancellation, shared-state contention, harder-to-debug
ordering) is not earned at this scale.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

CheckOutcome = Literal["pass", "warn", "fail"]

_VALID_OUTCOMES: frozenset[str] = frozenset(get_args(CheckOutcome))


@dataclass(frozen=True)
class Check:
    """A single health-check result.

    `name` is a stable snake_case identifier — users grep CI logs for
    these strings, so treat additions as additive and renames as
    breaking. `message` is the one-line human summary printed next to
    the pass/warn/fail glyph. `suggested_next` is optional remediation
    text shown only when present, typically omitted on `pass`.
    """

    name: str
    outcome: CheckOutcome
    message: str
    suggested_next: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Check.name must be a non-empty identifier")
        if not self.message:
            raise ValueError("Check.message must be a non-empty human-readable string")
        if self.outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"Check.outcome must be one of {sorted(_VALID_OUTCOMES)}; got {self.outcome!r}"
            )


@dataclass(frozen=True)
class CheckSummary:
    """Counts of each outcome across a `CheckResult`.

    Attribute names use `passed`/`warned`/`failed` rather than the raw
    outcome strings because `pass` is a reserved Python keyword. The
    JSON serialisation in `CheckResult.to_json_dict` translates back to
    the `pass`/`warn`/`fail` keys exposed via `schemabrain doctor --json`.
    """

    passed: int
    warned: int
    failed: int


@dataclass(frozen=True)
class CheckResult:
    """Aggregated outcome of a `doctor` run.

    Preserves the order the runner saw, so rendered output reads as a
    sequence the user can follow top-to-bottom. The `exit_code` is the
    single contract `doctor` returns to the shell.
    """

    checks: tuple[Check, ...]

    @property
    def summary(self) -> CheckSummary:
        passed = sum(1 for c in self.checks if c.outcome == "pass")
        warned = sum(1 for c in self.checks if c.outcome == "warn")
        failed = sum(1 for c in self.checks if c.outcome == "fail")
        return CheckSummary(passed=passed, warned=warned, failed=failed)

    @property
    def exit_code(self) -> int:
        # Warnings never break CI — see module docstring contract 2.
        return 1 if self.summary.failed > 0 else 0

    def to_json_dict(self) -> dict[str, Any]:
        """Render the result as a JSON-serialisable dict.

        Shape:

            {
              "checks": [
                {"name": str, "outcome": str, "message": str,
                 "suggested_next": str | null},
                ...
              ],
              "summary": {"pass": int, "warn": int, "fail": int},
              "exit_code": int
            }

        Stable key order (insertion-preserved) so the output is
        diff-friendly across runs.
        """
        s = self.summary
        return {
            "checks": [
                {
                    "name": c.name,
                    "outcome": c.outcome,
                    "message": c.message,
                    "suggested_next": c.suggested_next,
                }
                for c in self.checks
            ],
            "summary": {"pass": s.passed, "warn": s.warned, "fail": s.failed},
            "exit_code": self.exit_code,
        }


def run_checks(checks: Sequence[Callable[[], Check]]) -> CheckResult:
    """Run each check in order and collect results.

    A check that raises is caught here and converted into a `fail`
    Check. The synthesised name comes from the callable's `__name__`
    (falling back to a placeholder when that's missing or empty) so
    the user can still identify which check failed even when the
    callable itself never returned a Check. See module docstring
    contract 1.
    """
    results: list[Check] = []
    for check in checks:
        try:
            results.append(check())
        except Exception as exc:
            name = getattr(check, "__name__", "") or "unnamed_check"
            results.append(
                Check(
                    name=name,
                    outcome="fail",
                    message=f"check raised {type(exc).__name__}: {exc}",
                    suggested_next=(
                        "this is a bug in `schemabrain doctor`; "
                        "please open an issue with the full output"
                    ),
                )
            )
    return CheckResult(checks=tuple(results))


def render_json(result: CheckResult) -> str:
    """Render a `CheckResult` as a JSON string suitable for `doctor --json`.

    Two-space indent + sorted-keys=False to preserve the contract's
    insertion order; trailing newline so the output appends cleanly
    when piped to a file.
    """
    return json.dumps(result.to_json_dict(), indent=2) + "\n"
