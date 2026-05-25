"""Rich-rendered output for `schemabrain check`.

Kept separate from `engine` so the engine remains testable without
`rich` and so a future hosted-control-plane consumer can render the
same `CheckReport` shape its own way.

Output shape:

    SchemaBrain check — prod_warehouse
    8 entities (8 healthy) · 12 metrics (11 healthy) · 5 joins (5 healthy)

      ✓ 8 entities healthy
      ⚠ 1 metric drifted
      ✓ 5 joins healthy

      ✗ entity   customer
            identity_column_missing  public.customers.legacy_email
            → update entity `customer`'s `identity:` field and re-run ...

      ⚠ metric   total_revenue
            measure_column_missing  public.orders.total_cents
            → update metric `total_revenue`'s `measure.column` and ...

    1 drift detected.

Severity discipline:

  - Entity drift is a hard break — the agent cannot use the entity at
    all when its table or identity column is missing. Renders red ✗.
  - Metric and join drift degrade one definition without taking the
    rest of the semantic layer offline. Renders yellow ⚠.

This matches the v1 demo's tiered governance signal: ✗ blocks; ⚠
needs attention but doesn't stop the agent from answering against
the healthy definitions around it.
"""

from __future__ import annotations

import json

from rich.console import Console

from schemabrain._ui import GLYPH_OK, drift_glyph
from schemabrain.check.engine import CheckReport, Drift

_DEF_KIND_LABEL: dict[str, str] = {
    "entity": "entity",
    "metric": "metric",
    "canonical_join": "join",
}

# Drift severity (glyph + Rich style) routes through `schemabrain._ui`'s
# `drift_glyph(def_kind)` so the entity/metric/join → ✗/⚠/⚠ tiering
# lives in one place across the CLI. Adding a new `DriftKind` only
# requires extending the `_DRIFT_TIER` mapping in `_ui.py` — this
# renderer keeps reading the tier through one helper.


def render_report(
    report: CheckReport,
    *,
    console: Console,
    source_label: str,
) -> None:
    """Render the report to `console`.

    `source_label` is a short human identifier for the source the
    check ran against — typically the canonical (credential-stripped)
    source URL. Surfaces in the header so an operator running `check`
    against multiple sources can disambiguate at a glance.
    """
    drift_count = len(report.drifts)
    console.print(f"[bold]SchemaBrain check[/] — {source_label}")
    console.print(
        f"{report.total_entities} entit"
        f"{'y' if report.total_entities == 1 else 'ies'} "
        f"({report.entities_healthy} healthy) · "
        f"{report.total_metrics} metric"
        f"{'' if report.total_metrics == 1 else 's'} "
        f"({report.metrics_healthy} healthy) · "
        f"{report.total_joins} join"
        f"{'' if report.total_joins == 1 else 's'} "
        f"({report.joins_healthy} healthy)"
    )
    console.print()

    if not report.drifts:
        if report.total_entities + report.total_metrics + report.total_joins == 0:
            console.print(
                "[dim]No semantic-layer definitions in the store yet. "
                "Run `schemabrain entities suggest` or "
                "`schemabrain entities apply` to get started.[/]"
            )
        else:
            console.print("[green]All definitions match the live source.[/]")
        return

    # Per-type healthy / drifted summary lines so the operator sees at
    # a glance which surface is broken before scanning the per-drift
    # blocks. Entity-vs-metric-vs-join drift count is derived from the
    # report's drift list rather than carried as a separate field —
    # CheckReport.entities_healthy already reflects suppression
    # (cascaded metrics aren't double-counted as broken).
    _render_per_type_summary(report, console=console)
    console.print()

    for drift in report.drifts:
        _render_drift(drift, console=console)

    console.print()
    console.print(f"[red]{drift_count} drift{'' if drift_count == 1 else 's'} detected.[/]")


def _render_per_type_summary(report: CheckReport, *, console: Console) -> None:
    """Emit one summary line per definition type.

    Order mirrors the count line in the header (entities → metrics →
    joins) so the operator's eye moves down the same axis. When a
    type has zero definitions the line is suppressed — saying
    "0 joins healthy" on a project that hasn't curated joins is
    noise.

    Glyph + style resolve through `schemabrain._ui.drift_glyph` so
    a future `DriftKind` / `DefKind` addition routes to the right
    tier by extending `_DRIFT_TIER` in `_ui.py` rather than copy-
    pasting glyph logic at each call site.
    """
    for kind_singular, kind_plural, def_kind, total, healthy in (
        ("entity", "entities", "entity", report.total_entities, report.entities_healthy),
        ("metric", "metrics", "metric", report.total_metrics, report.metrics_healthy),
        ("join", "joins", "canonical_join", report.total_joins, report.joins_healthy),
    ):
        _summary_line(
            kind_singular=kind_singular,
            kind_plural=kind_plural,
            def_kind=def_kind,
            total=total,
            healthy=healthy,
            console=console,
        )


def _summary_line(
    *,
    kind_singular: str,
    kind_plural: str,
    def_kind: str,
    total: int,
    healthy: int,
    console: Console,
) -> None:
    """Emit one per-type healthy / drifted line.

    Suppresses the line entirely when `total == 0` so a project that
    hasn't curated this definition kind isn't told it has "0 joins
    healthy" — that's noise on a fresh store.

    Drift glyph + colour resolve through `drift_glyph(def_kind)`.
    Healthy lines always render green ✓ (`GLYPH_OK`) regardless of
    kind — success is a single tier, only drift has severity.
    """
    if total == 0:
        return
    drifted = total - healthy
    if drifted == 0:
        word = kind_singular if total == 1 else kind_plural
        console.print(f"  [green]{GLYPH_OK}[/] {total} {word} healthy")
    else:
        glyph, style = drift_glyph(def_kind)
        word = kind_singular if drifted == 1 else kind_plural
        console.print(
            f"  [{style}]{glyph}[/] {drifted} {word} drifted [dim]({healthy} of {total} healthy)[/]"
        )


def _render_drift(drift: Drift, *, console: Console) -> None:
    """One drift block: glyph + def_kind + def_name on line 1, indented
    detail + fix hint below.

    Glyph + colour resolve through `schemabrain._ui.drift_glyph` so
    entity drift renders as a hard red ✗ while metric / join drift
    renders as a yellow ⚠ (degraded but not blocking the rest of the
    semantic layer).
    """
    label = _DEF_KIND_LABEL.get(drift.def_kind, drift.def_kind)
    glyph, style = drift_glyph(drift.def_kind)
    console.print(f"  [{style}]{glyph}[/] [bold]{label}[/]  {drift.def_name}")
    console.print(f"        [yellow]{drift.drift_kind}[/]  [dim]{drift.detail}[/]")
    console.print(f"        [dim]→[/] {drift.fix_hint}")


def render_json(report: CheckReport) -> str:
    """Render a `CheckReport` as a JSON string for `check --json`.

    Two-space indent + trailing newline so the output appends cleanly
    when piped to a file. Insertion-order key preservation comes from
    `CheckReport.to_json_dict`.
    """
    return json.dumps(report.to_json_dict(), indent=2) + "\n"


__all__ = ["render_json", "render_report"]
