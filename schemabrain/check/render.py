"""Rich-rendered output for `schemabrain check`.

Kept separate from `engine` so the engine remains testable without
`rich` and so a future hosted-control-plane consumer can render the
same `CheckReport` shape its own way.

Output shape:

    Schema Brain check — prod_warehouse
    8 entities (8 healthy) · 12 metrics (11 healthy) · 5 joins (5 healthy)

      ✗ entity   customer
            column_missing  public.customers.legacy_email
            → update entity 'customer'`s `identity:` field and re-run ...

      ⚠ metric   total_revenue
            measure_column_missing  public.orders.total_cents
            → update metric 'total_revenue'`s `measure.column` and ...

    1 drift detected.

Drift glyph is `✗` (red) — a drift is always a hard problem the
operator has to act on. There is no warning shape; `check` is binary
("nothing has drifted" vs "this list has").
"""

from __future__ import annotations

import json

from rich.console import Console

from schemabrain.check.engine import CheckReport, Drift

_DEF_KIND_LABEL: dict[str, str] = {
    "entity": "entity",
    "metric": "metric",
    "canonical_join": "join",
}


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
    console.print(f"[bold]Schema Brain check[/] — {source_label}")
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

    for drift in report.drifts:
        _render_drift(drift, console=console)

    console.print()
    console.print(f"[red]{drift_count} drift{'' if drift_count == 1 else 's'} detected.[/]")


def _render_drift(drift: Drift, *, console: Console) -> None:
    """One drift block: glyph + def_kind + def_name on line 1, indented
    detail + fix hint below.
    """
    label = _DEF_KIND_LABEL.get(drift.def_kind, drift.def_kind)
    console.print(f"  [red]✗[/] [bold]{label}[/]  {drift.def_name}")
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
