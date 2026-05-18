"""Rich-rendered output for `schemabrain inspect`.

Separate from `engine` so the engine remains testable without `rich`
and so a future plain-text or hosted-control-plane consumer can
render the same dataclasses differently.

Two surfaces:

  - `render_summary` — no-arg `inspect` view (counts + name lists)
  - `render_entity_detail` — `inspect <name>` drill view
"""

from __future__ import annotations

from rich.console import Console

from schemabrain.inspect.engine import (
    AnchoredMetric,
    EntityColumnDetail,
    EntityDetail,
    RelatedEntity,
    StoreSummary,
)

_PII_GLYPH: dict[str, str] = {
    "public": "[dim]public[/]",
    "internal": "[yellow]internal[/]",
    "confidential": "[red]confidential[/]",
    "pii": "[red]pii[/]",
}


def render_summary(summary: StoreSummary, *, console: Console) -> None:
    """Render the no-arg `inspect` summary.

    Shape:

        Schema Brain inspect
        2 tables · 8 columns · 2 entities · 1 metric · 1 join

        Entities:
          customer
          order

        Metrics:
          total_revenue

        Joins:
          customer_orders

        Drill into one: schemabrain inspect <name>

    An empty store gets a one-line hint at the bottom directing the
    operator to `entities apply` or `entities suggest`.
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

    if summary.entity_names:
        console.print("[bold]Entities:[/]")
        for name in summary.entity_names:
            console.print(f"  {name}")
        console.print()

    if summary.metric_names:
        console.print("[bold]Metrics:[/]")
        for name in summary.metric_names:
            console.print(f"  {name}")
        console.print()

    if summary.join_names:
        console.print("[bold]Joins:[/]")
        for name in summary.join_names:
            console.print(f"  {name}")
        console.print()

    console.print("[dim]Drill into one: `schemabrain inspect <name>`[/]")


def render_entity_detail(detail: EntityDetail, *, console: Console) -> None:
    """Render the drill view for one entity.

    Shape:

        Entity: customer
        ------------------------------------------------------------
        Description:  A registered user who can place orders.
        Binding:      public.customers
        Identity:     id
        Origin:       manual

        Columns:
          id              bigint        not null  pk  identity  public
          email           text          not null            pii (contact)
          ...

        Related entities:
          order      outgoing  one_to_many  customer.id = order.customer_id
                     (via canonical join `customer_orders`)

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
    name_w = max(len(c.name) for c in columns)
    type_w = max(len(c.data_type) for c in columns)
    for col in columns:
        flags = []
        if col.is_primary_key:
            flags.append("[bold]pk[/]")
        if col.is_identity:
            flags.append("[bold cyan]identity[/]")
        flag_str = " ".join(flags) if flags else ""
        null_str = "[dim]not null[/]" if not col.nullable else "[dim]nullable[/]"
        pii = _PII_GLYPH.get(col.pii_sensitivity, col.pii_sensitivity)
        categories = f" [dim]({', '.join(col.pii_categories)})[/]" if col.pii_categories else ""
        console.print(
            f"  {col.name.ljust(name_w)}  "
            f"{col.data_type.ljust(type_w)}  "
            f"{null_str}  "
            f"{flag_str}  "
            f"{pii}{categories}"
        )


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
    for rel in related:
        cardinality = rel.cardinality or "[dim]?[/]"
        # Format `on` pairs as `this.col = other.col` from the
        # drilled entity's perspective; engine already normalised the
        # pair order so the first element is the local column.
        on_pretty = ", ".join(
            f"{this_entity}.{local} = {rel.name}.{remote}" for local, remote in rel.on
        )
        console.print(
            f"  {rel.name}  [dim]{rel.direction}[/]  {cardinality}  [dim]via `{rel.join_name}`[/]"
        )
        console.print(f"      [dim]{on_pretty}[/]")


def _render_metrics(
    metrics: tuple[AnchoredMetric, ...],
    *,
    console: Console,
) -> None:
    console.print("[bold]Anchored metrics:[/]")
    if not metrics:
        console.print("  [dim](no metrics anchored on this entity)[/]")
        return
    for m in metrics:
        time = m.time_dimension or "[dim]non-temporal[/]"
        grains = ", ".join(m.time_grains) if m.time_grains else "[dim]none[/]"
        console.print(
            f"  {m.name}  [dim]{m.agg}({m.column})[/]  [dim]time_dim={time} grains={grains}[/]"
        )


__all__ = ["render_entity_detail", "render_summary"]
