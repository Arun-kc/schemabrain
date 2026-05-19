"""Tests for `schemabrain.check.render` — the Rich-rendered output.

The engine has its own tests in `test_check_engine.py`; these focus on
the rendering layer's branching behavior. Two branches that the
all-drifted / all-healthy fixtures don't exercise:

  1. The per-type summary's "all healthy" line for a type that is
     fully healthy while another type has drift (entity healthy +
     metric drifted, etc.). The single-type-drifted fixtures only
     exercise the "drifted" branch.
  2. The per-type "all-types-healthy" suppression — covered by the
     existing happy-path tests against the engine; the renderer's
     `_summary_line` `total == 0` branch is also exercised here.
"""

from __future__ import annotations

import io

from rich.console import Console

from schemabrain.check.engine import CheckReport, Drift
from schemabrain.check.render import render_report


def _render_to_string(report: CheckReport) -> str:
    """Run `render_report` against an in-memory Console and return
    the captured output. `force_terminal=False` keeps Rich in
    plain-text mode (no SGR codes), so substring assertions land
    against the rendered glyphs and labels rather than escape
    sequences.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120)
    render_report(report, console=console, source_label="test-source")
    return buf.getvalue()


class TestPerTypeSummary:
    """Exercises `_render_per_type_summary` + `_summary_line` branches."""

    def test_mixed_drift_renders_healthy_line_for_healthy_type(self) -> None:
        """A report with healthy entities + drifted metrics must show
        BOTH a `✓ N entities healthy` line AND a `⚠ N metrics drifted`
        line. Pins the load-bearing UX promise that operators see
        which surface is broken at a glance — and which surfaces
        are NOT.

        Without this test the `_summary_line` healthy branch
        (drifted == 0) was uncovered because every existing
        test_check_engine fixture either had all healthy or all
        drifted, never the mixed shape.
        """
        report = CheckReport(
            drifts=(
                Drift(
                    def_kind="metric",
                    def_name="total_revenue",
                    drift_kind="measure_column_missing",
                    detail="public.orders.total_cents",
                    fix_hint="fix",
                ),
            ),
            entities_healthy=3,
            metrics_healthy=0,
            joins_healthy=2,
            total_entities=3,
            total_metrics=1,
            total_joins=2,
        )
        out = _render_to_string(report)
        # Per-type summary block: entities + joins are fully healthy;
        # metrics has one drifted.
        assert "3 entities healthy" in out
        assert "2 joins healthy" in out
        assert "1 metric drifted" in out
        # The detailed drift block follows the per-type summary.
        assert "metric" in out
        assert "total_revenue" in out
        # Final tally.
        assert "1 drift detected" in out

    def test_singular_healthy_label(self) -> None:
        """`_summary_line`'s singular/plural toggle picks `entity` for
        total==1 and `entities` otherwise. Pins both axes of the
        toggle so a future refactor (e.g. switching to inflect /
        a translations layer) cannot silently flip the wrong form.
        """
        report = CheckReport(
            drifts=(
                Drift(
                    def_kind="entity",
                    def_name="x",
                    drift_kind="table_missing",
                    detail="public.x",
                    fix_hint="fix",
                ),
            ),
            entities_healthy=0,
            metrics_healthy=1,
            joins_healthy=0,
            total_entities=1,
            total_metrics=1,
            total_joins=0,
        )
        out = _render_to_string(report)
        # One healthy metric → "metric" singular, not "metrics".
        assert "1 metric healthy" in out
        # Zero joins → suppressed (no "0 joins" line).
        assert "joins" not in out.split("1 drift")[0].split("metric")[-1]

    def test_zero_total_suppresses_summary_line(self) -> None:
        """When a type has zero definitions the per-type summary
        line is suppressed — saying "0 joins healthy" on a project
        that hasn't curated joins yet is noise. Verified via a
        report where joins_total == 0 and the rendered output
        contains no joins summary line.
        """
        report = CheckReport(
            drifts=(
                Drift(
                    def_kind="entity",
                    def_name="x",
                    drift_kind="table_missing",
                    detail="public.x",
                    fix_hint="fix",
                ),
            ),
            entities_healthy=0,
            metrics_healthy=0,
            joins_healthy=0,
            total_entities=1,
            total_metrics=0,
            total_joins=0,
        )
        out = _render_to_string(report)
        # The summary block exists (we have drift) but joins line
        # must not — total_joins == 0 means the suppression branch
        # in `_summary_line` returned without printing.
        assert "0 joins healthy" not in out
        assert "joins drifted" not in out
