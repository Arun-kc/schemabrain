"""Rich-rendered output for `schemabrain inspect`.

Separate from `engine` so the engine remains testable without `rich`
and so a future plain-text or hosted-control-plane consumer can
render the same dataclasses differently.

Two surfaces:

  - `render_summary` — no-arg `inspect` view (counts + grouped tree)
  - `render_entity_detail` — `inspect <name>` drill view

Both surfaces compose Rich's `Tree` (summary grouping) and `Table`
(column / related-entity / anchored-metric grids). The Tables use
`box.SIMPLE_HEAD` so column headers are underlined but row borders
stay invisible — reads as a clean grid in a terminal without the
boxed-in look of a default `Table()`.
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from schemabrain._ui import pii_marker
from schemabrain.inspect.engine import (
    AnchoredMetric,
    EntityColumnDetail,
    EntityDetail,
    RelatedEntity,
    StoreSummary,
)

# PII sensitivity markers route through `schemabrain._ui.pii_marker` so
# the rendered label vocabulary lives in one place across the CLI
# (today `inspect`; soon `doctor` and `audit` callers).


def render_summary(summary: StoreSummary, *, console: Console) -> None:
    """Render the no-arg `inspect` summary.

    Shape:

        Schema Brain inspect
        7 tables · 30 columns · 3 entities · 1 metric · 1 join

        Definitions
        ├── Entities (3)
        │   ├── customer
        │   ├── order
        │   └── product
        ├── Metrics (1)
        │   └── total_revenue
        └── Joins (1)
            └── customer_orders

        Drill into one: schemabrain inspect <name>

    An empty store gets a one-line hint at the bottom directing the
    operator to `entities apply` or `entities suggest`. Empty branches
    are omitted from the tree so a store with entities but no metrics
    does not render an empty "Metrics" subtree.
    """
    console.print("[bold]Schema Brain inspect[/]")
    # `column_count is None` is the cross-source sentinel — render
    # as "—" rather than 0 so the operator can tell "unknown" from
    # "no columns indexed." Pluralisation logic only applies when
    # we have a real integer.
    if summary.column_count is None:
        col_segment = "[dim]—[/] columns [dim](use --source to count)[/]"
    else:
        col_segment = f"{summary.column_count} column{'' if summary.column_count == 1 else 's'}"
    console.print(
        f"{summary.table_count} table"
        f"{'' if summary.table_count == 1 else 's'} · "
        f"{col_segment} · "
        f"{summary.entity_count} entit"
        f"{'y' if summary.entity_count == 1 else 'ies'} · "
        f"{summary.metric_count} metric"
        f"{'' if summary.metric_count == 1 else 's'} · "
        f"{summary.join_count} join"
        f"{'' if summary.join_count == 1 else 's'}"
    )
    console.print()

    if summary.entity_count == 0 and summary.metric_count == 0 and summary.join_count == 0:
        console.print(
            "[dim]No semantic-layer definitions in the store yet. "
            "Run `schemabrain entities suggest` or "
            "`schemabrain entities apply` to get started.[/]"
        )
        return

    tree = Tree("[bold]Definitions[/]", guide_style="dim")
    if summary.entity_names:
        branch = tree.add(f"[bold]Entities[/] [dim]({len(summary.entity_names)})[/]")
        for name in summary.entity_names:
            branch.add(name)
    if summary.metric_names:
        branch = tree.add(f"[bold]Metrics[/] [dim]({len(summary.metric_names)})[/]")
        for name in summary.metric_names:
            branch.add(name)
    if summary.join_names:
        branch = tree.add(f"[bold]Joins[/] [dim]({len(summary.join_names)})[/]")
        for name in summary.join_names:
            branch.add(name)
    console.print(tree)
    console.print()

    console.print("[dim]Drill into one: `schemabrain inspect <name>`[/]")


def render_entity_detail(detail: EntityDetail, *, console: Console) -> None:
    """Render the drill view for one entity.

    Shape:

        Entity: customer
        ────────────────────────────────────────────────────────────
        Description:  A registered user who can place orders.
        Binding:      public.customers
        Identity:     id
        Origin:       manual

        Columns:
          ┃ Name   ┃ Type    ┃ Null      ┃ Flags         ┃ PII
          ──────────────────────────────────────────────────────
            id       bigint    not null    pk  identity    public
            email    text      not null                    pii (contact)
            ...

        Related entities:
          ┃ Entity  ┃ Edge                  ┃ On
          ────────────────────────────────────────────────────────
            order    outgoing · one_to_many   customer.id = order.customer_id
                                              via `customer_orders`

        Anchored metrics:
          (none)
    """
    entity = detail.entity
    console.print(f"[bold]Entity:[/] {entity.name}")
    console.print("─" * 60)
    if entity.description:
        console.print(f"[dim]Description:[/]  {entity.description}")
    console.print(f"[dim]Binding:[/]      {entity.qualified_table}")
    console.print(f"[dim]Identity:[/]     {entity.identity}")
    console.print(f"[dim]Origin:[/]       {entity.origin}")
    console.print()

    _render_columns(detail.columns, console=console)
    console.print()
    _render_related(detail.related_entities, console=console, this_entity=entity.name)
    console.print()
    _render_metrics(detail.anchored_metrics, console=console)


def _render_columns(
    columns: tuple[EntityColumnDetail, ...],
    *,
    console: Console,
) -> None:
    console.print("[bold]Columns:[/]")
    if not columns:
        # The bound table is in the store's entity row but not in the
        # `tables` index — happens when `entities apply` ran but the
        # `tables` row hasn't been written yet (rare; primarily a
        # test or mid-migration shape).
        console.print("  [dim](bound table not indexed — run `schemabrain index` to refresh)[/]")
        return
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, padding=(0, 2), expand=False)
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Null")
    table.add_column("Flags")
    table.add_column("PII")
    for col in columns:
        flags: list[str] = []
        if col.is_primary_key:
            flags.append("[bold]pk[/]")
        if col.is_identity:
            flags.append("[bold cyan]identity[/]")
        flag_str = " ".join(flags) if flags else ""
        null_str = "[dim]not null[/]" if not col.nullable else "[dim]nullable[/]"
        pii_cell = pii_marker(col.pii_sensitivity)
        if col.pii_categories:
            pii_cell = f"{pii_cell} [dim]({', '.join(col.pii_categories)})[/]"
        table.add_row(col.name, col.data_type, null_str, flag_str, pii_cell)
    console.print(table)


def _render_related(
    related: tuple[RelatedEntity, ...],
    *,
    console: Console,
    this_entity: str,
) -> None:
    console.print("[bold]Related entities:[/]")
    if not related:
        console.print("  [dim](no canonical joins involve this entity)[/]")
        return
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, padding=(0, 2), expand=False)
    table.add_column("Entity", style="bold")
    table.add_column("Edge")
    table.add_column("On")
    for rel in related:
        cardinality = rel.cardinality or "?"
        edge_cell = f"[dim]{rel.direction}[/] · {cardinality}"
        # Format `on` pairs as `this.col = other.col` from the
        # drilled entity's perspective; engine already normalised the
        # pair order so the first element is the local column.
        on_pretty = ", ".join(
            f"{this_entity}.{local} = {rel.name}.{remote}" for local, remote in rel.on
        )
        on_cell = f"{on_pretty}\n[dim]via `{rel.join_name}`[/]"
        table.add_row(rel.name, edge_cell, on_cell)
    console.print(table)


def _render_metrics(
    metrics: tuple[AnchoredMetric, ...],
    *,
    console: Console,
) -> None:
    console.print("[bold]Anchored metrics:[/]")
    if not metrics:
        console.print("  [dim](no metrics anchored on this entity)[/]")
        return
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, padding=(0, 2), expand=False)
    table.add_column("Metric", style="bold")
    table.add_column("Aggregation")
    table.add_column("Time")
    for m in metrics:
        agg_cell = f"[dim]{m.agg}({m.column})[/]"
        time = m.time_dimension or "[dim]non-temporal[/]"
        grains = ", ".join(m.time_grains) if m.time_grains else "[dim]none[/]"
        time_cell = f"{time} [dim]grains={grains}[/]"
        table.add_row(m.name, agg_cell, time_cell)
    console.print(table)


__all__ = ["render_entity_detail", "render_summary"]
