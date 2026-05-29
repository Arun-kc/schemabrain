"""Rich-rendered output for `schemabrain inspect`.

Separate from `engine` so the engine remains testable without `rich`
and so a future plain-text or hosted-control-plane consumer can
render the same dataclasses differently.

Two surfaces:

  - `render_summary` — no-arg `inspect` view (brand line + tree
    + 3 compact summary panels)
  - `render_entity_detail` — `inspect <name>` drill view (brand
    line + columns table + related/metrics)

Both surfaces compose Rich's `Tree` (summary grouping) and `Table`
(column / related-entity / anchored-metric grids). The Tables use
`box.SIMPLE_HEAD` so column headers are underlined but row borders
stay invisible — reads as a clean grid in a terminal without the
boxed-in look of a default `Table()`.

The brand lines (``◆ store · <path>`` for summary, ``◆ <qualified
table>`` for drill) are the design-system anchor that ties this
surface to the wizard hero, doctor checklist, error renderers,
and `init --help` formatter shipped in prior PRs of the
migration arc. Pure visual layer — no domain logic in the
brand line composition.
"""

from __future__ import annotations

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from schemabrain._ui import GLYPH_BRAND, GLYPH_SEP, pii_marker, short_path
from schemabrain.core.entity import Entity
from schemabrain.inspect.engine import (
    AnchoredMetric,
    EntityColumnDetail,
    EntityDetail,
    JoinDetail,
    MetricDetail,
    RelatedEntity,
    StoreSummary,
)

# PII sensitivity markers route through `schemabrain._ui.pii_marker` so
# the rendered label vocabulary lives in one place across the CLI
# (today `inspect`; soon `doctor` and `audit` callers).

# How many comma-separated names render in each summary panel before
# we collapse to `name · name · … (N more)`. 12 strikes the balance
# between giving the eye real signal (a handful of representative
# names) and not hijacking the panel for a list that the operator
# should drill into with `inspect <name>` anyway.
_SUMMARY_PANEL_NAME_LIMIT: int = 12


def render_summary(
    summary: StoreSummary, *, console: Console, store_path: str | None = None
) -> None:
    """Render the no-arg `inspect` summary.

    Shape:

        ◆ store · ~/.schemabrain/acme/store.db
        8 tables · 39 columns · 3 entities · 1 metric · 1 join

        Definitions
        ├── Entities (3)
        │   ├── customer
        │   ├── order
        │   └── product
        ├── Metrics (1)
        │   └── total_revenue
        └── Joins (1)
            └── customer_orders

        ╭ entities · 3 ╮  ╭ metrics · 1 ╮  ╭ joins · 1 ╮
        │ customer ·   │  │ total_       │  │ customer_  │
        │ order ·      │  │ revenue      │  │ orders     │
        │ product      │  │              │  │            │
        ╰──────────────╯  ╰──────────────╯  ╰────────────╯

        Drill into one: schemabrain inspect <name>

    An empty store gets a one-line hint at the bottom directing the
    operator to `entities apply` or `entities suggest`. Empty branches
    are omitted from the tree so a store with entities but no metrics
    does not render an empty "Metrics" subtree.

    ``store_path`` is the local SQLite store path; rendered in the
    brand line so an operator running `inspect` against multiple
    stores can tell them apart at a glance. ``None`` omits the path
    suffix — useful in tests + future hosted-control-plane callers
    that don't have a single canonical store-on-disk path.
    """
    console.print(_compose_summary_brand_line(store_path))
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
    # When the store holds data from more than one source-id (typically:
    # pre-existing rows from an older schemabrain version's
    # `_canonical_url` plus fresh rows from the current version),
    # surface a banner so the operator can tell why the counts above
    # are larger than they expected from a single `init` run. The
    # renderer already dedupes names cross-source, so this banner is
    # the ONLY visual signal a second source-id exists in the store.
    if len(summary.source_connection_ids) > 1:
        console.print(
            f"[yellow]⚠[/] store has data from "
            f"[bold]{len(summary.source_connection_ids)}[/] source connections; "
            f"names above are deduped. "
            f"[dim]Pass [bold]--source URL[/bold] to scope to one source, "
            f"or `rm` the store and re-run `schemabrain init` to start clean "
            f"if the extra source-id is orphan data from a previous version.[/]"
        )
    console.print()

    if summary.entity_count == 0 and summary.metric_count == 0 and summary.join_count == 0:
        console.print(
            "[dim]No semantic-layer definitions in the store yet. "
            "Run `schemabrain entities suggest` or "
            "`schemabrain entities apply` to get started.[/]"
        )
        # Empty-state users need the discovery links MORE than
        # populated-store users — they're the new operator who
        # just ran `init` without an API key and is figuring out
        # what to do next. Render the same 3-line block so they
        # see `doctor`/`check`/`tail` regardless of store contents.
        console.print()
        _render_discovery_block(console=console)
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

    console.print(Columns(_build_summary_panels(summary), equal=True, expand=True))
    console.print()

    console.print("[dim]Drill into one: `schemabrain inspect <name>`[/]")
    console.print()
    _render_discovery_block(console=console)


def _render_discovery_block(*, console: Console) -> None:
    """Render the 3-line `doctor`/`check`/`tail` discovery block.

    Day-one UX overhaul: discovery links to the other commands a
    new user would benefit from. `inspect` is often the first
    command run after `init`, so this is the highest-leverage
    place to surface the rest of the post-init surface. Rendered
    as a compact 3-line block — short and skimmable, not a wall
    of recommendations. Shared between the populated-store and
    empty-store branches so empty-state users (the most
    confused cohort) see it too.
    """
    console.print("[dim]Verify wiring:  `schemabrain doctor`[/]")
    console.print("[dim]Detect drift:   `schemabrain check`[/]")
    console.print("[dim]Watch traffic:  `schemabrain tail --follow`[/]")


def _compose_summary_brand_line(store_path: str | None) -> Text:
    """``◆ store · <path>`` brand line.

    Cyan brand glyph + bold ``store`` label + dim path. Matches the
    design's surface header (handoff bundle
    ``cli/operator.jsx:53``). The path renders through
    ``_ui.short_path`` so a user-home prefix collapses to ``~/`` —
    consistent with the doctor renderer's brand line and the
    dry-run cost-estimate panel's ``store`` row. Without the
    collapse, terminal recordings + CI logs + support
    screenshots would leak the operator's OS username on the
    visible path.
    """
    text = Text()
    text.append(GLYPH_BRAND, style="cyan")
    text.append(" ")
    text.append("store", style="bold")
    if store_path:
        text.append(f" {GLYPH_SEP} ", style="bright_black")
        text.append(short_path(store_path), style="dim")
    return text


def _build_summary_panels(summary: StoreSummary) -> list[Panel]:
    """Build the 3 ``entities`` / ``metrics`` / ``joins`` summary panels.

    Always renders three panels even when a category is empty —
    matches the design's mock (handoff bundle
    ``cli/operator.jsx:66-74``) which keeps the grid balanced so
    an empty-state surface teaches the operator what they don't
    have yet (``metrics · 0  (none yet)``). The hollow-panel
    concern that drove the prior "skip empty" version is
    addressed by rendering an explicit ``(none yet)`` body so
    the panel reads as intentional empty-state, not a UI glitch.
    """
    return [
        _summary_panel("entities", summary.entity_names),
        _summary_panel("metrics", summary.metric_names),
        _summary_panel("joins", summary.join_names),
    ]


def _summary_panel(label: str, names: tuple[str, ...]) -> Panel:
    """One ``label · count`` panel summarising a definition family.

    Renders ``(none yet)`` body for empty categories so the
    three-panel grid stays balanced — see ``_build_summary_panels``
    docstring for the design rationale.
    """
    if names:
        visible = list(names[:_SUMMARY_PANEL_NAME_LIMIT])
        extra = len(names) - len(visible)
        body_parts = visible[:]
        if extra > 0:
            body_parts.append(f"… ({extra} more)")
        body = " · ".join(body_parts)
    else:
        body = "[dim](none yet)[/]"
    # Explicit ``Text`` construction (not f-string markup) so an
    # accidental bracket character in ``label`` wouldn't be
    # interpreted as a Rich style directive. Label values are
    # hardcoded today (``entities``/``metrics``/``joins``) but
    # the explicit form documents the safe pattern for callers.
    title = Text()
    title.append(label, style="dim")
    title.append(f" · {len(names)}", style="bold")
    return Panel(body, title=title, title_align="left", padding=(0, 1))


def render_entity_detail(detail: EntityDetail, *, console: Console) -> None:
    """Render the drill view for one entity.

    Shape:

        ◆ public.customers · entity:customer · binding id

        Description:  A registered user who can place orders.

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

    The brand line replaces the old ``Entity: <name>`` +
    dashed-rule pair. Columns / related / metrics tables are
    unchanged — the data shape they render is what operators have
    been grepping CI logs for since v0.2.x and we don't reshape
    it inside this PR.
    """
    entity = detail.entity
    console.print(_compose_entity_brand_line(entity))
    console.print()
    if entity.description:
        console.print(f"[dim]Description:[/]  {entity.description}")
        console.print()

    _render_columns(detail.columns, console=console)
    console.print()
    _render_related(detail.related_entities, console=console, this_entity=entity.name)
    console.print()
    _render_metrics(detail.anchored_metrics, console=console)
    console.print()
    # Trust line at the bottom mirrors the metric + join drill views;
    # without it, operators inspecting an entity got no surface signal
    # for whether the entity's identity/binding was LLM-suggested vs
    # FK-derived vs hand-authored — even though the data was present.
    _render_trust_line(
        inference_method=entity.inference_method,
        validation_state=entity.validation_state,
        console=console,
    )


def _compose_entity_brand_line(entity: Entity) -> Text:
    """``◆ <qualified_table> · entity:<name> · binding <identity_col>`` brand line.

    Pulls the entity's display essentials (qualified-table
    binding, entity name as an ``entity:<name>`` tag mirroring the
    summary tree, identity column) into one cyan-led line so the
    drill view leads with identity rather than the old
    ``Entity: <name>`` + chrome-rule pair. The qualified table
    name carries schema + table so multi-schema stores stay
    unambiguous.

    Typed against the concrete ``Entity`` dataclass (not ``object``)
    so a future attribute rename (eg ``qualified_table`` →
    ``binding_table``) surfaces at mypy / pyright time rather than
    silently rendering a hollow ``◆  · entity: · binding`` line.
    """
    text = Text()
    text.append(GLYPH_BRAND, style="cyan")
    text.append(" ")
    text.append(entity.qualified_table, style="bold")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(f"entity:{entity.name}", style="green")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(f"binding {entity.identity}", style="dim")
    if entity.origin != "manual":
        text.append(f" {GLYPH_SEP} ", style="bright_black")
        text.append(f"origin {entity.origin}", style="dim")
    return text


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
    # `overflow="fold"` lets the bridge-name line wrap across multiple
    # display rows instead of truncating with an ellipsis when the
    # synthesised name (`<a>_<b>_via_<junction>`) exceeds the column
    # width Rich picks for narrow terminals.
    table.add_column("On", overflow="fold")
    for rel in related:
        cardinality = rel.cardinality or "?"
        edge_cell = f"[dim]{rel.direction}[/] · {cardinality}"
        if rel.via_junction is not None:
            # Bridges read M:N from the drilled side; the on-pairs
            # describe the FIRST leg only (drilled → junction), so the
            # column-equality string is rendered against the junction
            # rather than the far end. The second-line provides the
            # bridge name + junction-mediated nature explicitly so the
            # operator never confuses a bridge with a direct join.
            on_pretty = ", ".join(
                f"{this_entity}.{local} = {rel.via_junction}.{remote}" for local, remote in rel.on
            )
            on_cell = f"{on_pretty}\n[dim]bridge `{rel.join_name}` via {rel.via_junction}[/]"
        else:
            # Format `on` pairs as `this.col = other.col` from the
            # drilled entity's perspective; engine already normalised
            # the pair order so the first element is the local column.
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
        # `column` and `expression` are mutually exclusive (XOR) — pick
        # whichever populated field describes the measure shape. Bare
        # columns render as `sum(amount)`; composite expressions render
        # as `sum(unit_price * quantity)`.
        measure_body = m.column if m.column is not None else m.expression
        agg_cell = f"[dim]{m.agg}({measure_body})[/]"
        time = m.time_dimension or "[dim]non-temporal[/]"
        grains = ", ".join(m.time_grains) if m.time_grains else "[dim]none[/]"
        time_cell = f"{time} [dim]grains={grains}[/]"
        table.add_row(m.name, agg_cell, time_cell)
    console.print(table)


def render_metric_detail(detail: MetricDetail, *, console: Console) -> None:
    """Render the drill view for one metric.

    Shape:

        ◆ metric:total_revenue · entity:order (public.orders) · origin suggested

        Description:  Sum of order totals.

        Measure:      sum(total_cents)
        Time:         order.created_at — grains: day, week, month
        Trust:        llm_suggested · applied (MEDIUM)

    The brand line leads with `metric:<name>` mirroring the entity
    drill's `entity:<name>` so the operator sees the same shape
    regardless of which kind they drilled into. Anchor table is
    surfaced in parentheses next to the anchor entity name — answers
    "where does the measure column live?" without a second drill.
    """
    metric = detail.metric
    text = Text()
    text.append(GLYPH_BRAND, style="cyan")
    text.append(" ")
    text.append(f"metric:{metric.name}", style="bold")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    anchor_label = f"entity:{metric.entity}"
    if detail.anchor is not None:
        anchor_label = f"{anchor_label} ({detail.anchor.qualified_table})"
    else:
        anchor_label = f"{anchor_label} [red](orphaned)[/]"
    text.append(anchor_label, style="green")
    if metric.origin != "manual":
        text.append(f" {GLYPH_SEP} ", style="bright_black")
        text.append(f"origin {metric.origin}", style="dim")
    console.print(text)
    console.print()
    if metric.description:
        console.print(f"[dim]Description:[/]  {metric.description}")
        console.print()

    measure_body = (
        metric.measure.column if metric.measure.column is not None else metric.measure.expression
    )
    console.print(f"[bold]Measure:[/]      [dim]{metric.measure.agg}({measure_body})[/]")
    if metric.time_dimension is not None:
        grain_str = ", ".join(metric.time_grains) if metric.time_grains else "[dim]none[/]"
        console.print(f"[bold]Time:[/]         {metric.time_dimension} [dim]grains: {grain_str}[/]")
    else:
        console.print("[bold]Time:[/]         [dim]non-temporal[/]")
    _render_trust_line(
        inference_method=metric.inference_method,
        validation_state=metric.validation_state,
        console=console,
    )


def render_join_detail(detail: JoinDetail, *, console: Console) -> None:
    """Render the drill view for one canonical join.

    Shape:

        ◆ join:customer_orders · order ← customer · cardinality many_to_one

        Description:  ...

        On:           order.user_id = customer.id
        Trust:        fk_constraint · applied (HIGH)
    """
    join = detail.join
    text = Text()
    text.append(GLYPH_BRAND, style="cyan")
    text.append(" ")
    text.append(f"join:{join.name}", style="bold")
    text.append(f" {GLYPH_SEP} ", style="bright_black")
    text.append(f"{join.source_entity} → {join.target_entity}", style="green")
    if join.cardinality is not None:
        text.append(f" {GLYPH_SEP} ", style="bright_black")
        text.append(f"cardinality {join.cardinality}", style="dim")
    if join.origin != "manual":
        text.append(f" {GLYPH_SEP} ", style="bright_black")
        text.append(f"origin {join.origin}", style="dim")
    console.print(text)
    console.print()
    if join.description:
        console.print(f"[dim]Description:[/]  {join.description}")
        console.print()

    pretty_on = "  AND  ".join(
        f"{join.source_entity}.{p.source_column} = {join.target_entity}.{p.target_column}"
        for p in join.on
    )
    console.print(f"[bold]On:[/]           {pretty_on}")
    if detail.source is not None and detail.target is not None:
        console.print(
            f"[bold]Tables:[/]       {detail.source.qualified_table} ↔ "
            f"{detail.target.qualified_table}"
        )
    _render_trust_line(
        inference_method=join.inference_method,
        validation_state=join.validation_state,
        console=console,
    )


def _render_trust_line(
    *,
    inference_method: str,
    validation_state: str,
    console: Console,
) -> None:
    """Render the Charter v1.2 2D trust signal as a single labelled line.

    Computes the derived confidence label so the operator sees both
    the raw 2D signal (`fk_constraint · applied`) AND the bucket the
    MCP envelope would report (`HIGH`). Import is local because the
    envelope module pulls in Pydantic transitively — keeping it
    off the module-load path of the otherwise-light inspect renderer.
    """
    from schemabrain.mcp.envelope import derive_confidence

    confidence = derive_confidence(inference_method, validation_state)  # type: ignore[arg-type]
    confidence_style = {
        "HIGH": "green",
        "MEDIUM": "yellow",
        "LOW": "red",
    }.get(confidence, "")
    console.print(
        f"[bold]Trust:[/]        {inference_method} {GLYPH_SEP} {validation_state} "
        f"[{confidence_style}]({confidence})[/]"
    )


__all__ = [
    "render_entity_detail",
    "render_join_detail",
    "render_metric_detail",
    "render_summary",
]
