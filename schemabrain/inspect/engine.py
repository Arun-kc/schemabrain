"""Data builders for `schemabrain inspect`.

Reads only from the local `Store`; never touches a live source. The
`StoreSummary` shape backs the no-args summary view, and
`EntityDetail` backs the `inspect <name>` drill view.

Design choices:

  - **Columns are returned in ordinal-position order.** Same order the
    user sees in `\\d table` in psql; the renderer doesn't have to
    sort. The store's `get_table` already orders by ordinal_position.
  - **Related-entities traversal is bidirectional.** Drilling into
    `customer` surfaces every join where `customer` is `source_entity`
    OR `target_entity`. The `direction` field on `RelatedEntity` lets
    the renderer flip arrow direction in the output.
  - **PII tags are eagerly resolved** via one `get_column_pii_tags`
    call per entity. Columns without a stored row default to
    `("public", ())` — matches the propagation helper's empty-input
    contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from schemabrain.core.entity import Entity
from schemabrain.core.join import Cardinality
from schemabrain.core.metric import AggFunction
from schemabrain.core.store_protocol import Store
from schemabrain.pii.categories import Sensitivity

# Closed direction values — `outgoing` from the drilled entity's
# perspective means the entity is the join's `source_entity`;
# `incoming` means it is the `target_entity`.
Direction = Literal["outgoing", "incoming"]
_VALID_DIRECTIONS: frozenset[str] = frozenset(get_args(Direction))


@dataclass(frozen=True)
class EntityColumnDetail:
    """One column on an entity's bound table, enriched with the
    entity-level identity flag + the PII tags persisted at index time.

    `pii_sensitivity` carries the closed `Sensitivity` Literal from
    `pii/categories.py` (4 values) — narrower than `str` so a
    renderer's glyph table cannot silently fall through to an
    arbitrary value.
    """

    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool
    is_identity: bool
    pii_sensitivity: Sensitivity
    pii_categories: tuple[str, ...]


@dataclass(frozen=True)
class RelatedEntity:
    """One join edge from the perspective of the entity being drilled.

    `direction == "outgoing"` means the drilled entity is the join's
    `source_entity`; `"incoming"` means it is the `target_entity`.
    `on` is `(local_column, remote_column)` pairs from the drilled
    entity's perspective — the renderer doesn't have to flip them.
    `cardinality` mirrors `CanonicalJoin.cardinality` (4-value
    Literal, or `None` for back-compat with joins authored before
    the column existed).
    """

    name: str
    direction: Direction
    join_name: str
    on: tuple[tuple[str, str], ...]
    cardinality: Cardinality | None

    def __post_init__(self) -> None:
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"direction must be one of {sorted(_VALID_DIRECTIONS)} (got {self.direction!r})"
            )


@dataclass(frozen=True)
class AnchoredMetric:
    """A metric anchored on the entity being drilled.

    `agg` carries the closed `AggFunction` Literal from `core/metric.py`
    so the renderer can format aggregations safely; widening to `str`
    here would silently allow an unvalidated value through if a future
    caller bypassed `MetricMeasure`'s validation.
    """

    name: str
    description: str
    agg: AggFunction
    column: str
    time_dimension: str | None
    time_grains: tuple[str, ...]


@dataclass(frozen=True)
class EntityDetail:
    """Full inspect view for one entity."""

    entity: Entity
    columns: tuple[EntityColumnDetail, ...]
    related_entities: tuple[RelatedEntity, ...]
    anchored_metrics: tuple[AnchoredMetric, ...]


@dataclass(frozen=True)
class StoreSummary:
    """Top-level inspect summary across the store.

    `column_count` is `None` when the summary was built without a
    source-id filter — column counts can't be aggregated cross-source
    without confusing partial totals, so the engine returns the
    sentinel instead of a misleading 0. The renderer surfaces this
    as "—" so the operator can tell "unknown" from "no columns".

    `source_connection_ids` lists the distinct sources that have data
    in the store. Populated only on the unfiltered (cross-source)
    build; the scoped build leaves it as a single-element tuple
    matching the supplied filter, or empty when filter is `None` and
    the store is empty. The renderer uses `len > 1` to surface a
    multi-source warning banner — smoke 2026-05-19 surfaced an
    operator whose pre-existing store had 6 entities under an older
    source-id scheme and 6 fresh entities under the new scheme, with
    no visual signal that 12 rendered names were 6 logical entities
    duplicated.

    When the summary is cross-source, `entity_names` / `metric_names`
    / `join_names` are DEDUPED by name — the operator-facing answer
    to "what's curated" should not list the same name twice. The
    `*_count` fields match the deduped name counts so the header
    line reads consistently with the lists below it. To inspect the
    raw row count per source, pass `--source URL` to scope the
    summary.
    """

    table_count: int
    column_count: int | None
    entity_count: int
    metric_count: int
    join_count: int
    entity_names: tuple[str, ...]
    metric_names: tuple[str, ...]
    join_names: tuple[str, ...]
    source_connection_ids: tuple[str, ...] = ()


def build_summary(*, store: Store, source_connection_id: str | None) -> StoreSummary:
    """Compute a `StoreSummary` across the store.

    `source_connection_id=None` aggregates across every source;
    supplying a value scopes counts to that source — matches the
    semantics of `list_entities` / `list_metrics` / `list_canonical_joins`.

    Cross-source builds (filter is `None`) DEDUPE entity / metric /
    join names — multiple source-ids that share a name surface as
    one tree entry plus a multi-source banner the renderer composes
    from `source_connection_ids`. The count fields mirror the deduped
    name lists so the header line ("12 entities") doesn't disagree
    with the tree below it ("6 names"). See the `StoreSummary`
    docstring for why this matters in practice.
    """
    qualified_tables = store.list_tables(source_connection_id=source_connection_id)
    entities = store.list_entities(source_connection_id=source_connection_id)
    metrics = store.list_metrics(source_connection_id=source_connection_id)
    joins = store.list_canonical_joins(source_connection_id=source_connection_id)

    # Column count requires walking every table — for a small store
    # this is one cheap round-trip per table; if this ever becomes a
    # hot path (10k-table stores), promote it to a `Store.count_columns`
    # method. Today the inspect view is operator-facing, not on the
    # MCP call path, so a 7-table demo paying ~7 ms is fine.
    column_count: int | None
    if source_connection_id is None:
        # `get_table` requires a `source_connection_id`, and a
        # cross-source aggregation would risk double-counting if
        # two sources happen to share `(schema, table)` pairs.
        # Return the sentinel so the renderer can show "—" instead
        # of a misleading 0.
        column_count = None
    else:
        column_count = 0
        for schema, name in qualified_tables:
            table = store.get_table(schema, name, source_connection_id=source_connection_id)
            if table is None:  # pragma: no cover — invariant guard
                continue
            column_count += len(table.columns)

    if source_connection_id is None:
        # Cross-source: dedupe names so the tree reads as the
        # operator's mental model ("what's curated?") rather than as
        # raw rows. Counts mirror the deduped lists so the header
        # line agrees with the tree.
        entity_names = tuple(sorted({e.name for e in entities}))
        metric_names = tuple(sorted({m.name for m in metrics}))
        join_names = tuple(sorted({j.name for j in joins}))
        entity_count = len(entity_names)
        metric_count = len(metric_names)
        join_count = len(join_names)
        # Tables already enumerate distinct (schema, name) globally,
        # so deduping by qualified name catches the same-table-name
        # collision across sources symmetrically.
        deduped_tables = {f"{schema}.{name}" for schema, name in qualified_tables}
        table_count = len(deduped_tables)
        # `list_distinct_source_connection_ids` is the new Store
        # protocol method (PR fix smoke 2026-05-19); spans tables +
        # entities + metrics + canonical_joins so an old store with
        # data in only one of those tables still surfaces.
        source_ids = tuple(store.list_distinct_source_connection_ids())
    else:
        # Scoped: raw row counts equal distinct-name counts under the
        # `(source_connection_id, name)` PK on each table, so the
        # `len(rows)` form preserves the original v0 semantics.
        entity_names = tuple(sorted(e.name for e in entities))
        metric_names = tuple(sorted(m.name for m in metrics))
        join_names = tuple(sorted(j.name for j in joins))
        entity_count = len(entities)
        metric_count = len(metrics)
        join_count = len(joins)
        table_count = len(qualified_tables)
        source_ids = (source_connection_id,)

    return StoreSummary(
        table_count=table_count,
        column_count=column_count,
        entity_count=entity_count,
        metric_count=metric_count,
        join_count=join_count,
        entity_names=entity_names,
        metric_names=metric_names,
        join_names=join_names,
        source_connection_ids=source_ids,
    )


def build_entity_detail(
    *,
    store: Store,
    entity_name: str,
    source_connection_id: str,
) -> EntityDetail | None:
    """Return a full `EntityDetail` for `entity_name`, or `None` if
    no entity with that name exists under `source_connection_id`.

    A `source_connection_id` is required here — unlike `build_summary`,
    the column-walk for the drilled entity needs to resolve the bound
    table against a specific source.
    """
    entity = store.get_entity(entity_name, source_connection_id=source_connection_id)
    if entity is None:
        return None

    # `Entity.__post_init__` enforces `schema.table` form via
    # `_QUALIFIED_TABLE_RE`, so a well-formed store row always splits
    # cleanly. The defensive guard catches the (rare) case of a
    # tampered store row whose `qualified_table` somehow lost its
    # dot — surface a clear assertion rather than a `ValueError`
    # from tuple unpacking.
    parts = entity.qualified_table.split(".", 1)
    if len(parts) != 2:  # pragma: no cover — invariant guard
        raise AssertionError(
            f"entity {entity_name!r} has malformed qualified_table "
            f"{entity.qualified_table!r}; expected 'schema.table' form"
        )
    schema, table_name = parts
    table = store.get_table(schema, table_name, source_connection_id=source_connection_id)
    columns: tuple[EntityColumnDetail, ...] = ()
    if table is not None:
        column_names = [c.name for c in table.columns]
        pii_tags = store.get_column_pii_tags(
            source_connection_id=source_connection_id,
            qualified_table=entity.qualified_table,
            columns=column_names,
        )
        details = []
        for col in table.columns:
            sensitivity, categories = pii_tags.get(col.name, ("public", frozenset()))
            details.append(
                EntityColumnDetail(
                    name=col.name,
                    data_type=col.data_type,
                    nullable=col.nullable,
                    is_primary_key=col.is_primary_key,
                    is_identity=col.name == entity.identity,
                    pii_sensitivity=sensitivity,
                    pii_categories=tuple(sorted(categories)),
                )
            )
        columns = tuple(details)

    related_entities = _resolve_related_entities(
        store=store,
        entity_name=entity_name,
        source_connection_id=source_connection_id,
    )
    anchored_metrics = _resolve_anchored_metrics(
        store=store,
        entity_name=entity_name,
        source_connection_id=source_connection_id,
    )

    return EntityDetail(
        entity=entity,
        columns=columns,
        related_entities=related_entities,
        anchored_metrics=anchored_metrics,
    )


def _resolve_related_entities(
    *,
    store: Store,
    entity_name: str,
    source_connection_id: str,
) -> tuple[RelatedEntity, ...]:
    """Walk every join under `source_connection_id` and surface those
    that touch `entity_name`. Direction + on-pair orientation are
    normalised to the drilled entity's perspective so renderers don't
    have to flip anything.
    """
    joins = store.list_canonical_joins(source_connection_id=source_connection_id)
    related: list[RelatedEntity] = []
    for join in joins:
        if join.source_entity == entity_name:
            related.append(
                RelatedEntity(
                    name=join.target_entity,
                    direction="outgoing",
                    join_name=join.name,
                    on=tuple((p.source_column, p.target_column) for p in join.on),
                    cardinality=join.cardinality,
                )
            )
        elif join.target_entity == entity_name:
            related.append(
                RelatedEntity(
                    name=join.source_entity,
                    direction="incoming",
                    join_name=join.name,
                    # Flip the pair so it reads
                    # `(local_column, remote_column)` from the drilled
                    # entity's perspective.
                    on=tuple((p.target_column, p.source_column) for p in join.on),
                    cardinality=join.cardinality,
                )
            )
    # Stable alphabetical ordering by related-entity name so the
    # render is deterministic across runs.
    related.sort(key=lambda r: (r.name, r.join_name))
    return tuple(related)


def _resolve_anchored_metrics(
    *,
    store: Store,
    entity_name: str,
    source_connection_id: str,
) -> tuple[AnchoredMetric, ...]:
    """Return the metrics anchored on `entity_name`, alphabetised."""
    metrics = store.list_metrics(source_connection_id=source_connection_id)
    anchored = [m for m in metrics if m.entity == entity_name]
    return tuple(
        AnchoredMetric(
            name=m.name,
            description=m.description,
            agg=m.measure.agg,
            column=m.measure.column,
            time_dimension=m.time_dimension,
            time_grains=tuple(m.time_grains),
        )
        for m in sorted(anchored, key=lambda x: x.name)
    )


__all__ = [
    "AnchoredMetric",
    "EntityColumnDetail",
    "EntityDetail",
    "RelatedEntity",
    "StoreSummary",
    "build_entity_detail",
    "build_summary",
]
