"""Metric request resolver.

Turns `(metric_name, group_by, filters, time_grain, limit)` into a
`MetricPlan` by looking up the metric, its anchor entity, and any
canonical joins it touches. Raises structured `MetricCompilerError`
subclasses when the request can't be satisfied — the MCP tool layer
maps each subclass to a charter envelope `kind`.

At v1 the resolver handles ONE-HOP joins only — multi-hop is a v2
query-planner concern. Any group_by or filter column on an entity that
isn't 1-hop reachable from the metric's anchor raises
`UnreachableEntityError`.
"""

from __future__ import annotations

import re
from typing import Any

from schemabrain.core.metric import Metric, TimeGrain
from schemabrain.core.store_protocol import Store
from schemabrain.semantic.compiler.plan import (
    AmbiguousJoinError,
    InvalidTimeGrainError,
    MalformedColumnError,
    MetricPlan,
    Operator,
    RequestedFilter,
    ResolvedColumn,
    ResolvedJoin,
    ResolvedPredicate,
    UnknownColumnError,
    UnknownMetricError,
    UnreachableEntityError,
)

# Same `<entity>.<column>` shape used by `Metric.time_dimension` —
# exactly one dot, both sides identifier-shaped. The compiler accepts
# the same alphabet so a metric author and a get_metric caller can
# reference columns the same way.
_QUALIFIED_COLUMN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_$]*)\.([A-Za-z_][A-Za-z0-9_$]*)$")

# Unary operators ignore `value`; `in` / `not_in` require list-shaped
# values; the remainder require scalar values.
_UNARY_OPS: frozenset[str] = frozenset({"is_null", "not_null"})
_LIST_OPS: frozenset[str] = frozenset({"in", "not_in"})


def resolve_metric_plan(
    *,
    store: Store,
    source_connection_id: str,
    metric_name: str,
    group_by: tuple[str, ...] = (),
    filters: tuple[RequestedFilter, ...] = (),
    time_grain: TimeGrain | None = None,
    limit: int = 1000,
) -> MetricPlan:
    """Compile a metric request into a `MetricPlan`.

    Raises a `MetricCompilerError` subclass on any structural failure;
    the MCP tool maps each subclass to a charter envelope `kind`.

    `time_grain=None` against a temporal metric is a valid request —
    the result is unbucketed (one row across the whole window). A
    `time_grain` value not in `metric.time_grains` is rejected with
    `InvalidTimeGrainError`.

    `group_by` items are deduplicated by `(entity, column)` so the
    same column twice doesn't duplicate JOINs in the emitted SQL.
    """
    metric = store.get_metric(metric_name, source_connection_id=source_connection_id)
    if metric is None:
        raise UnknownMetricError(
            f"metric {metric_name!r} is not defined for this source; "
            f"run `schemabrain metrics list` to see available metrics."
        )

    _check_time_grain(metric, time_grain)
    anchor_alias = _alias_for(metric.entity)
    anchor_table = _lookup_anchor_table(store, metric.entity, source_connection_id)

    # Per-entity resolution cache: `target_entity → ResolvedJoin`. The
    # first reference to a non-anchor entity does the canonical-join
    # lookup + builds the ResolvedJoin; subsequent references reuse it
    # so a group_by and a filter on the same joined entity share one
    # JOIN clause and one alias.
    resolved_joins: dict[str, ResolvedJoin] = {}

    def _resolve(column_ref: str, kind: str) -> ResolvedColumn:
        # `kind` is "group_by" or "filter" — surfaces in the
        # MalformedColumnError message so the user knows which
        # argument is at fault.
        match = _QUALIFIED_COLUMN_RE.fullmatch(column_ref)
        if match is None:
            raise MalformedColumnError(
                f"{kind} column {column_ref!r} must be in 'entity.column' "
                f"form (e.g. 'order.created_at')"
            )
        entity_name, column = match.group(1), match.group(2)
        if entity_name == metric.entity:
            return ResolvedColumn(
                entity=entity_name,
                column=column,
                qualified_table=anchor_table,
                alias=anchor_alias,
            )
        if entity_name in resolved_joins:
            join = resolved_joins[entity_name]
            return ResolvedColumn(
                entity=entity_name,
                column=column,
                qualified_table=join.target_table,
                alias=join.target_alias,
            )
        # First time we've seen this entity in the request — resolve
        # the canonical join from anchor.
        joins = store.resolve_canonical_joins(
            metric.entity,
            entity_name,
            source_connection_id=source_connection_id,
        )
        if not joins:
            target_table = _lookup_optional_table(store, entity_name, source_connection_id)
            if target_table is None:
                raise UnknownColumnError(
                    f"{kind} column {column_ref!r} references entity "
                    f"{entity_name!r} which is not defined for this source"
                )
            raise UnreachableEntityError(
                anchor_entity=metric.entity,
                target_entity=entity_name,
            )
        if len(joins) > 1:
            raise AmbiguousJoinError(
                anchor_entity=metric.entity,
                target_entity=entity_name,
                candidate_join_names=tuple(j.name for j in joins),
            )
        canonical_join = joins[0]
        target_table = _lookup_anchor_table(store, entity_name, source_connection_id)
        resolved = ResolvedJoin(
            canonical_name=canonical_join.name,
            target_entity=entity_name,
            target_table=target_table,
            target_alias=_alias_for(entity_name),
            on_pairs=canonical_join.on,
            cardinality=canonical_join.cardinality,
        )
        resolved_joins[entity_name] = resolved
        return ResolvedColumn(
            entity=entity_name,
            column=column,
            qualified_table=target_table,
            alias=resolved.target_alias,
        )

    # Resolve group_by columns, deduplicating by (entity, column) so
    # the same column twice doesn't produce duplicate JOINs or duplicate
    # SELECT columns. Order-preserving via dict insertion order.
    group_by_resolved: dict[tuple[str, str], ResolvedColumn] = {}
    for column_ref in group_by:
        col = _resolve(column_ref, kind="group_by")
        group_by_resolved.setdefault((col.entity, col.column), col)

    # Resolve filter predicates.
    filter_predicates: list[ResolvedPredicate] = []
    for index, requested in enumerate(filters):
        col = _resolve(requested.column, kind="filter")
        _check_operator_value(requested.op, requested.value, index)
        param_names = _param_names_for(requested.op, index, requested.value)
        filter_predicates.append(
            ResolvedPredicate(
                column=col,
                op=requested.op,
                param_names=param_names,
                value=requested.value,
            )
        )

    # Sort joins by canonical name so emit + provenance are
    # deterministic regardless of resolution order.
    sorted_joins = tuple(
        sorted(resolved_joins.values(), key=lambda j: j.canonical_name)
    )

    return MetricPlan(
        metric=metric,
        anchor_table=anchor_table,
        anchor_alias=anchor_alias,
        group_by_columns=tuple(group_by_resolved.values()),
        time_bucket=time_grain,
        filter_predicates=tuple(filter_predicates),
        limit=limit,
        joins=sorted_joins,
    )


# ----- helpers ---------------------------------------------------------------


def _check_time_grain(metric: Metric, time_grain: TimeGrain | None) -> None:
    if time_grain is None:
        return
    if not metric.time_grains:
        raise InvalidTimeGrainError(
            requested_grain=time_grain,
            allowed_grains=(),
        )
    if time_grain not in metric.time_grains:
        raise InvalidTimeGrainError(
            requested_grain=time_grain,
            allowed_grains=metric.time_grains,
        )


def _check_operator_value(op: Operator, value: Any, index: int) -> None:
    if op in _UNARY_OPS:
        if value is not None:
            raise MalformedColumnError(
                f"filter[{index}] operator {op!r} is unary; value must be omitted "
                f"(got {value!r})"
            )
        return
    if op in _LIST_OPS:
        if not isinstance(value, list):
            raise MalformedColumnError(
                f"filter[{index}] operator {op!r} expects a list value "
                f"(got {type(value).__name__}: {value!r})"
            )
        if not value:
            raise MalformedColumnError(
                f"filter[{index}] operator {op!r} requires a non-empty list value"
            )
        return
    # Scalar ops — value must be present and non-list.
    if value is None:
        raise MalformedColumnError(
            f"filter[{index}] operator {op!r} requires a non-null value; "
            f"use 'is_null' for null checks"
        )
    if isinstance(value, list):
        raise MalformedColumnError(
            f"filter[{index}] operator {op!r} requires a scalar value "
            f"(got list: {value!r}); use 'in' / 'not_in' for list values"
        )


def _param_names_for(op: Operator, index: int, value: Any) -> tuple[str, ...]:
    if op in _UNARY_OPS:
        return ()
    if op in _LIST_OPS:
        # value is guaranteed list + non-empty by _check_operator_value
        return tuple(f"p_filter_{index}_{j}" for j in range(len(value)))
    return (f"p_filter_{index}",)


def _alias_for(entity_name: str) -> str:
    # Entity names are identifier-shaped (`Entity.__post_init__`
    # enforces it), so they're safe to use as aliases verbatim.
    return entity_name


def _lookup_anchor_table(
    store: Store, entity_name: str, source_connection_id: str
) -> str:
    """Look up the qualified table for a known entity, asserting presence.

    Used for the metric's anchor (FK guarantees it exists) and for
    canonical-join target entities (FK on `canonical_joins` guarantees
    both ends exist). A `None` return indicates store corruption.
    """
    entity = store.get_entity(entity_name, source_connection_id=source_connection_id)
    if entity is None:  # pragma: no cover — FK guarantee
        raise RuntimeError(
            f"store corruption: metric anchor or join target {entity_name!r} "
            f"is referenced but the `entities` row is missing"
        )
    return entity.qualified_table


def _lookup_optional_table(
    store: Store, entity_name: str, source_connection_id: str
) -> str | None:
    """Look up a potentially-missing entity, used for UnknownColumnError
    vs UnreachableEntityError disambiguation."""
    entity = store.get_entity(entity_name, source_connection_id=source_connection_id)
    if entity is None:
        return None
    return entity.qualified_table


