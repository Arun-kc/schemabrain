"""Assemble a `DictionaryModel` from the store — the only store reader.

`build_dictionary` walks one source (or every source, sorted) and, per
entity, gathers its physical columns + PII tags + descriptions, anchors
its canonical joins (on either endpoint) and metrics, and renders each
join's ``ON`` clause with catastrophic-leak FK column names masked.

Anchoring mirrors the sidecar's algorithm (`dashboard/sidecar.py`):
joins surface on both endpoints; metrics anchor on `metric.entity`.
Ordering is the store's existing deterministic orderings (entities,
joins, metrics all by name; columns by ordinal position) so the rendered
output is stable across processes — the byte-exact golden depends on it.

This is the only module in the package that imports the store. The
renderers consume the returned model and never touch SQLite.
"""

from __future__ import annotations

from schemabrain.core.entity import Entity
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.metric import Metric
from schemabrain.core.store import SCHEMA_VERSION
from schemabrain.core.store_protocol import Store
from schemabrain.datadict.model import (
    DictColumn,
    DictEntity,
    DictionaryModel,
    DictJoin,
    DictMetric,
    DictSource,
)
from schemabrain.mcp._redaction import render_join_on_clause
from schemabrain.pii import PII_CATEGORIES_ORDERED

# Human label for an entity/join's `inference_method`. Unknown values
# fall through verbatim so a future taxonomy addition degrades cleanly.
_PROVENANCE_LABELS: dict[str, str] = {
    "fk_constraint": "FK-derived",
    "manually_authored": "Operator-authored",
    "llm_suggested": "Inferred",
    "observed_in_query_log": "Log-mined",
    "dbt_import": "Imported",
}


def _provenance(inference_method: str) -> str:
    return _PROVENANCE_LABELS.get(inference_method, inference_method)


def _build_columns(store: Store, sid: str, entity: Entity) -> tuple[DictColumn, ...]:
    schema_name, table_name = entity.qualified_table.split(".", 1)
    table = store.get_table(schema_name, table_name, source_connection_id=sid)
    if table is None:  # pragma: no cover - defensive
        # Unreachable under the store's FK guarantee: `entities` has a
        # foreign key onto `tables`, so an entity's bound table is always
        # indexed. Kept as a belt-and-suspenders guard against a future
        # store backend that breaks the cascade (mirrors sidecar.py).
        return ()
    columns = sorted(table.columns, key=lambda c: c.ordinal_position)
    column_names = [col.name for col in columns]
    tags = store.get_column_pii_tags(
        source_connection_id=sid,
        qualified_table=entity.qualified_table,
        columns=column_names,
    )
    descriptions = store.get_table_descriptions(schema_name, table_name, source_connection_id=sid)
    result: list[DictColumn] = []
    for col in columns:
        sensitivity, categories = tags.get(col.name, ("public", frozenset()))
        # Iterate the ordered tuple, never the frozenset — frozenset
        # iteration is hash-seed dependent and would destabilise the golden.
        ordered_categories = tuple(c for c in PII_CATEGORIES_ORDERED if c in categories)
        description = descriptions.get(col.name)
        result.append(
            DictColumn(
                name=col.name,
                data_type=col.data_type,
                nullable=col.nullable,
                is_primary_key=col.is_primary_key,
                is_identity=col.name == entity.identity,
                description=description.text if description is not None else None,
                pii_sensitivity=sensitivity,
                pii_categories=ordered_categories,
            )
        )
    return tuple(result)


def _build_joins(
    store: Store,
    sid: str,
    entity: Entity,
    all_joins: list[CanonicalJoin],
    entity_by_name: dict[str, Entity],
) -> tuple[DictJoin, ...]:
    return tuple(
        DictJoin(
            name=join.name,
            description=join.description,
            source_entity=join.source_entity,
            target_entity=join.target_entity,
            on_clause=render_join_on_clause(
                join,
                store=store,
                source_connection_id=sid,
                entity_by_name=entity_by_name,
            ),
            cardinality=join.cardinality,
            provenance=_provenance(join.inference_method),
        )
        for join in all_joins
        if entity.name in (join.source_entity, join.target_entity)
    )


def _build_metrics(entity: Entity, all_metrics: list[Metric]) -> tuple[DictMetric, ...]:
    result: list[DictMetric] = []
    for metric in all_metrics:
        if metric.entity != entity.name:
            continue
        measure = metric.measure.column or metric.measure.expression
        assert measure is not None  # MetricMeasure enforces column XOR expression
        result.append(
            DictMetric(
                name=metric.name,
                description=metric.description,
                agg=metric.measure.agg,
                measure=measure,
                measure_is_expression=metric.measure.expression is not None,
                time_dimension=metric.time_dimension,
                time_grains=metric.time_grains,
            )
        )
    return tuple(result)


def _build_entity(
    store: Store,
    sid: str,
    entity: Entity,
    all_joins: list[CanonicalJoin],
    all_metrics: list[Metric],
    entity_by_name: dict[str, Entity],
) -> DictEntity:
    return DictEntity(
        name=entity.name,
        description=entity.description,
        qualified_table=entity.qualified_table,
        identity=entity.identity,
        group=entity.group,
        origin=entity.origin,
        inference_method=entity.inference_method,
        validation_state=entity.validation_state,
        columns=_build_columns(store, sid, entity),
        joins=_build_joins(store, sid, entity, all_joins, entity_by_name),
        metrics=_build_metrics(entity, all_metrics),
    )


def _build_source(store: Store, sid: str) -> DictSource:
    entities = store.list_entities(source_connection_id=sid)
    entity_by_name = {entity.name: entity for entity in entities}
    all_joins = store.list_canonical_joins(source_connection_id=sid)
    # Propagates MalformedMetricRowError (a ValueError) on a corrupt row —
    # the CLI maps it to a guided "corrupt store" exit rather than dropping
    # a metric silently.
    all_metrics = store.list_metrics(source_connection_id=sid)
    return DictSource(
        source_connection_id=sid,
        entities=tuple(
            _build_entity(store, sid, entity, all_joins, all_metrics, entity_by_name)
            for entity in entities
        ),
    )


def build_dictionary(*, store: Store, source_connection_id: str | None) -> DictionaryModel:
    """Build the full data-dictionary model for one source, or every source.

    `source_connection_id=None` walks every indexed source in sorted id
    order (yielding an empty model for an empty store). A specific id
    scopes to that one source.
    """
    if source_connection_id is None:
        source_ids = sorted(store.list_distinct_source_connection_ids())
    else:
        source_ids = [source_connection_id]
    return DictionaryModel(
        schema_version=SCHEMA_VERSION,
        sources=tuple(_build_source(store, sid) for sid in source_ids),
    )
