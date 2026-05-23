"""Metric request resolver.

Turns `(metric_name, group_by, filters, time_grain, limit, via)` into a
`MetricPlan` by looking up the metric, its anchor entity, and the
chain of canonical joins needed to reach each referenced entity.
Raises structured `MetricCompilerError` subclasses when the request
can't be satisfied — the MCP tool layer maps each subclass to a
charter envelope `kind`.

Multi-hop reachability: the resolver BFSes the canonical-join graph
(treating each canonical join as an undirected edge between its
`source_entity` and `target_entity`) for the shortest path from the
metric's anchor to each referenced entity. Intermediate hops on the
path become additional `ResolvedJoin` entries in the plan, in
topological chain order. Path-level ambiguity (two paths of equal
length) refuses with `AmbiguousPathError`; the agent disambiguates
via `via=(join_name,)`.
"""

from __future__ import annotations

import dataclasses
import re
from collections import deque
from typing import Any, Literal

from schemabrain.core.join import CanonicalJoin, JoinColumnPair, flip_cardinality
from schemabrain.core.metric import Metric, TimeGrain
from schemabrain.core.store_protocol import Store
from schemabrain.semantic.compiler.plan import (
    AmbiguousJoinError,
    AmbiguousPathError,
    AmbiguousTimeDimensionError,
    InvalidTimeGrainError,
    MalformedColumnError,
    MetricPlan,
    Operator,
    RequestedFilter,
    RequestedOrderBy,
    ResolvedColumn,
    ResolvedJoin,
    ResolvedOrderBy,
    ResolvedPredicate,
    UnknownColumnError,
    UnknownFilterColumnError,
    UnknownGroupByColumnError,
    UnknownMeasureColumnError,
    UnknownMetricError,
    UnknownOrderByColumnError,
    UnknownViaJoinError,
    UnreachableEntityError,
)

# Closed grammar of column-validation surfaces. Annotating
# `_validate_column_on_table(kind=...)` and `_resolve(kind=...)` as
# this Literal lets the type checker reject a future caller that
# passes (say) `"order_by"` without first wiring the right error class
# into the branch ladder. Runtime correctness is also defended by the
# explicit `if`/`if`/`if` ladder + final `RuntimeError`.
ColumnValidationKind = Literal["group_by", "filter", "measure"]

# Same `<entity>.<column>` shape used by `Metric.time_dimension` —
# exactly one dot, both sides identifier-shaped. The compiler accepts
# the same alphabet so a metric author and a get_metric caller can
# reference columns the same way.
_QUALIFIED_COLUMN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_$]*)\.([A-Za-z_][A-Za-z0-9_$]*)$")

# Unary operators ignore `value`; `in` / `not_in` require list-shaped
# values; the remainder require scalar values.
_UNARY_OPS: frozenset[str] = frozenset({"is_null", "not_null"})
_LIST_OPS: frozenset[str] = frozenset({"in", "not_in"})

# Bound on echoed metric_name in `UnknownMetricError`. Three times
# Postgres NAMEDATALEN-1 (3 * 63 = 189) is generous headroom for any
# legitimate name while still capping an attacker-controlled echo at a
# few hundred bytes. Parallel to `_MAX_ECHO_LEN` in `mcp/_helpers.py`.
_MAX_METRIC_NAME_ECHO = 200


def resolve_metric_plan(
    *,
    store: Store,
    source_connection_id: str,
    metric_name: str,
    group_by: tuple[str, ...] = (),
    filters: tuple[RequestedFilter, ...] = (),
    time_grain: TimeGrain | None = None,
    time_dimension: str | None = None,
    limit: int = 1000,
    via: tuple[str, ...] = (),
    order_by: tuple[RequestedOrderBy, ...] = (),
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

    `via` is a tuple of canonical-join names the caller wants the
    chain to pass through. Used to disambiguate `AmbiguousPathError`
    or `AmbiguousJoinError`. Each name MUST be a canonical join that
    appears on a candidate path from anchor to a referenced entity;
    otherwise `UnknownViaJoinError` is raised.

    `order_by` lists ORDER BY clauses the caller wants emitted.
    Each entry's `column` must be either the metric's `name` (the
    SELECT-aliased measure) OR one of the `group_by` columns;
    anything else raises `UnknownOrderByColumnError`. When at least
    one ORDER BY clause is set AND `group_by` is non-empty, the
    resolver auto-appends a deterministic tie-breaker (first
    group-by column ASC, flagged via `auto_appended_tie_break=True`)
    so identical measure values produce identical row order across
    runs. When `order_by` is empty AND `group_by` is non-empty, the
    resolver auto-fills `order_by` with every group column ASC so
    the LIMIT N slice is deterministic without the caller having to
    construct one. When both are empty (single-row aggregate), no
    ORDER BY is emitted.

    `time_dimension` is the v1.2 inheritance disambiguator. When the
    metric has no local `time_dimension` and 2+ timestamp columns are
    reachable via canonical joins, the resolver filters the candidate
    set to the caller-specified `<entity>.<column>`. A filter that
    leaves 0 candidates still raises `AmbiguousTimeDimensionError` so
    the agent sees the full valid set (the message guides them to a
    real choice). When the metric has its own declared
    `time_dimension`, the arg is ignored — the metric's declared
    dimension always wins.
    """
    metric = store.get_metric(metric_name, source_connection_id=source_connection_id)
    if metric is None:
        # Bounded echo: a 100 KB metric_name from a prompt-injected
        # agent would otherwise round-trip back into the caller's
        # context window through this error string. Parallel to
        # `_bounded_repr` in `mcp/_helpers.py`; inlined here rather
        # than imported to dodge a circular dependency via
        # `mcp/__init__` → `get_metric` → `semantic.compiler`.
        echoed = (
            metric_name
            if len(metric_name) <= _MAX_METRIC_NAME_ECHO
            else (metric_name[:_MAX_METRIC_NAME_ECHO] + "...")
        )
        raise UnknownMetricError(
            f"metric {echoed!r} is not defined for this source; "
            f"run `schemabrain metrics list` to see available metrics."
        )

    # When the metric DECLARES a time_dimension, validate the requested
    # grain against the metric's allowed grains as before. When the
    # metric has no time_dimension, defer the validation to the
    # inheritance step below — the inherited dimension supports the
    # full grain set so a v1.2 caller can bucket against a reachable
    # timestamp column without the metric author having to pre-declare.
    if metric.time_dimension is not None:
        _check_time_grain(metric, time_grain)
    anchor_alias = _alias_for(metric.entity)
    anchor_table = _lookup_anchor_table(store, metric.entity, source_connection_id)

    # Lazy-load the canonical-join graph: built once on first reference
    # to a non-anchor entity, reused for every subsequent BFS in this
    # request. For requests that only reference the anchor (no group_by,
    # filters on anchor only) we skip the store roundtrip entirely.
    graph: _JoinGraph | None = None

    # Cache of `target_entity → ResolvedJoin` for every entity reached
    # so far in this request, INCLUDING intermediate hops. A second
    # group_by referencing the same intermediate (e.g. group_by
    # ["user.email", "address.country"] both transit `order`) reuses
    # the same JOIN — one alias, one ON clause.
    resolved_joins: dict[str, ResolvedJoin] = {}

    via_set = frozenset(via)
    # `consumed_via` accumulates which via names actually constrained
    # a hop in some chain. Each `_find_canonical_chain` returns its
    # consumed names alongside the chain; the caller merges into this
    # request-level set ONLY on success, so partial-chain consumed
    # names from a chain that later raised don't pollute the shared
    # set (defends a future retry/catch refactor from silently
    # suppressing real `UnknownViaJoinError` raises).
    consumed_via: set[str] = set()

    # Cache of `qualified_table → frozenset[column_name]` for every
    # table this request touched, so the column-existence check below
    # never does a redundant store roundtrip when the same entity is
    # referenced more than once (group_by + filter on user.email, etc.).
    columns_by_table: dict[str, frozenset[str]] = {}

    def _ensure_graph() -> _JoinGraph:
        nonlocal graph
        if graph is None:
            edges = store.list_canonical_joins(source_connection_id=source_connection_id)
            graph = _build_join_graph(edges)
        return graph

    def _columns_on(qualified_table: str) -> frozenset[str]:
        # Lazy fetch + cache. `qualified_table` is `<schema>.<name>` —
        # the same shape `entity.qualified_table` uses everywhere else,
        # validated by `Entity.__post_init__`. Splitting on the first
        # dot mirrors `_lookup_anchor_table`'s caller contract.
        cached = columns_by_table.get(qualified_table)
        if cached is not None:
            return cached
        schema_name, table_name = qualified_table.split(".", 1)
        table = store.get_table(
            schema_name=schema_name,
            name=table_name,
            source_connection_id=source_connection_id,
        )
        # `table is None` here is store corruption — same shape as
        # `_lookup_anchor_table`'s defence. Resolution reached this
        # line because the entity was found and its qualified_table
        # was returned, so the underlying tables row should exist.
        if table is None:  # pragma: no cover — store corruption
            raise RuntimeError(
                f"store corruption: entity references table "
                f"{qualified_table!r} but the `tables` row is missing"
            )
        names = frozenset(col.name for col in table.columns)
        columns_by_table[qualified_table] = names
        return names

    def _validate_column_on_table(
        *,
        entity: str,
        column: str,
        qualified_table: str,
        kind: ColumnValidationKind,
    ) -> None:
        # `kind` is "group_by" / "filter" / "measure" — picks the right
        # error class so the MCP envelope distinguishes between the
        # surfaces. ORDER BY has its own alias-based validation in
        # the order_by resolution block below.
        allowed = _columns_on(qualified_table)
        if column in allowed:
            return
        sorted_allowed = tuple(sorted(allowed))
        if kind == "group_by":
            raise UnknownGroupByColumnError(
                entity=entity,
                column=column,
                allowed_columns=sorted_allowed,
            )
        if kind == "filter":
            raise UnknownFilterColumnError(
                entity=entity,
                column=column,
                allowed_columns=sorted_allowed,
            )
        if kind == "measure":
            raise UnknownMeasureColumnError(
                entity=entity,
                column=column,
                allowed_columns=sorted_allowed,
            )
        # No other `kind` value reaches the validator today — keep the
        # branch tight so a future surface is forced to declare its
        # error class explicitly instead of inheriting whichever class
        # happened to be last in the if-chain.
        raise RuntimeError(  # pragma: no cover — defensive
            f"unreachable: _validate_column_on_table called with kind={kind!r}"
        )

    def _resolve(column_ref: str, kind: ColumnValidationKind) -> ResolvedColumn:
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
            _validate_column_on_table(
                entity=entity_name,
                column=column,
                qualified_table=anchor_table,
                kind=kind,
            )
            return ResolvedColumn(
                entity=entity_name,
                column=column,
                qualified_table=anchor_table,
                alias=anchor_alias,
            )
        if entity_name in resolved_joins:
            join = resolved_joins[entity_name]
            _validate_column_on_table(
                entity=entity_name,
                column=column,
                qualified_table=join.target_table,
                kind=kind,
            )
            return ResolvedColumn(
                entity=entity_name,
                column=column,
                qualified_table=join.target_table,
                alias=join.target_alias,
            )
        # First time we've seen this entity in the request — BFS the
        # canonical-join graph from anchor to here. Intermediate hops
        # already in `resolved_joins` are reused; only new hops produce
        # new ResolvedJoin entries.
        g = _ensure_graph()
        target_table_check = _lookup_optional_table(store, entity_name, source_connection_id)
        if target_table_check is None:
            raise UnknownColumnError(
                f"{kind} column {column_ref!r} references entity "
                f"{entity_name!r} which is not defined for this source"
            )
        chain, chain_consumed_via = _find_canonical_chain(
            graph=g,
            anchor=metric.entity,
            target=entity_name,
            via=via_set,
        )
        # Chain resolved successfully — merge its consumed names into
        # the request-level set. If the BFS raised, this point is never
        # reached, so partial-chain consumed names don't pollute the
        # shared set.
        consumed_via.update(chain_consumed_via)
        # `chain` is the ordered list of (predecessor_entity, edge)
        # tuples. For each hop, either reuse a cached ResolvedJoin or
        # build a fresh one anchored on the predecessor's alias.
        for predecessor, edge in chain:
            if edge.target_entity_in_chain in resolved_joins:
                continue
            target_table = _lookup_anchor_table(
                store, edge.target_entity_in_chain, source_connection_id
            )
            source_alias = (
                anchor_alias
                if predecessor == metric.entity
                else resolved_joins[predecessor].target_alias
            )
            # Cardinality reported on `ResolvedJoin` is the EFFECTIVE
            # value in the chain's traversal direction, not the
            # stored direction. When the BFS walked the canonical
            # join from target→source (reverse of stored
            # source→target), the cardinality flips so downstream
            # consumers (fan-out detector, plan emitter) see the
            # shape that actually applies to the SQL they emit. The
            # direction signal comes from `_ChainEdge.is_reverse_traversal`
            # (set deterministically at graph-build time) rather than
            # an on-pair comparison — same-column-name FK joins would
            # have silently bypassed an on-pair check.
            effective_cardinality = (
                flip_cardinality(edge.join.cardinality)
                if edge.is_reverse_traversal
                else edge.join.cardinality
            )
            resolved_joins[edge.target_entity_in_chain] = ResolvedJoin(
                canonical_name=edge.join.name,
                source_alias=source_alias,
                target_entity=edge.target_entity_in_chain,
                target_table=target_table,
                target_alias=_alias_for(edge.target_entity_in_chain),
                on_pairs=edge.on_pairs_in_chain_direction,
                cardinality=effective_cardinality,
            )
        # Final ResolvedJoin for the requested entity is now cached.
        join = resolved_joins[entity_name]
        _validate_column_on_table(
            entity=entity_name,
            column=column,
            qualified_table=join.target_table,
            kind=kind,
        )
        return ResolvedColumn(
            entity=entity_name,
            column=column,
            qualified_table=join.target_table,
            alias=join.target_alias,
        )

    # Validate every column the metric's measure references against the
    # anchor table's column list. Closes a silent-failure path that
    # existed even for v1 bare-column measures: a typo in
    # `MetricMeasure.column` used to surface only at Postgres execution
    # time as `UndefinedColumn`, wrapped as `internal_error`. With
    # composite expressions referencing multiple operands, the surface
    # widens — every operand needs the same check. `measure_columns`
    # returns the set of all column names referenced regardless of
    # which shape (bare / composite) populated the dataclass.
    for measure_column in sorted(metric.measure.measure_columns):
        _validate_column_on_table(
            entity=metric.entity,
            column=measure_column,
            qualified_table=anchor_table,
            kind="measure",
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

    # `consumed_via` tracking exists for future symmetry with a v2
    # multi-target retry contract; today, `_find_canonical_chain`'s
    # `issubset` filter rejects any chain whose path doesn't contain
    # every via name, so any successful resolution already guarantees
    # every via name was consumed. No end-of-call orphan check needed.

    # `joins` is emitted in topological chain order. Insertion order
    # of `resolved_joins` is already topological because `_resolve`
    # walks the BFS path from anchor outward, inserting each hop only
    # if not already present.
    ordered_joins = tuple(resolved_joins.values())

    # Resolve ORDER BY clauses. The allowed SELECT-alias set is the
    # metric's name (the measure aggregate's alias) plus a `group_col_N`
    # alias for each resolved group_by column. We map each caller-
    # supplied reference to its alias here so the emitter can output
    # `ORDER BY <alias> <DIRECTION>` directly (no second column-resolution
    # pass at emit time). The alias mapping is also what enables the
    # tie-break logic below.
    group_by_columns_ordered = tuple(group_by_resolved.values())
    alias_by_request: dict[str, str] = {metric.name: metric.name}
    for index, group_col in enumerate(group_by_columns_ordered):
        alias_by_request[f"{group_col.entity}.{group_col.column}"] = f"group_col_{index}"

    # When the caller passed `group_by` but no `order_by`, default
    # ORDER BY to the group columns themselves so the LIMIT N slice is
    # deterministic without forcing every agent to explicitly construct
    # one. Without this, every `get_metric(group_by=[...])` call would
    # surface `missing_order_by_with_limit` as a degradation — a
    # legitimate determinism concern, but firing on every grouped query
    # erodes the meaning of `degraded` and trains agents to ignore it.
    # The explicit-`order_by` path is untouched: callers who specify
    # any ORDER BY clause keep full control over ranking (with the
    # tie-break logic below adding a deterministic suffix).
    if not order_by and group_by_columns_ordered:
        order_by = tuple(
            RequestedOrderBy(
                column=f"{group_col.entity}.{group_col.column}",
                direction="asc",
            )
            for group_col in group_by_columns_ordered
        )

    order_by_resolved: list[ResolvedOrderBy] = []
    seen_aliases: set[str] = set()
    for requested in order_by:
        alias = alias_by_request.get(requested.column)
        if alias is None:
            raise UnknownOrderByColumnError(
                requested_column=requested.column,
                allowed_columns=tuple(alias_by_request.keys()),
            )
        if alias in seen_aliases:
            # Caller asked to order by the same alias twice. The second
            # clause is silently dropped — duplicate ORDER BY entries
            # have no SQL effect anyway, and refusing here would be
            # noisier than the duplicate deserves. Track this as a
            # potential future degradation_reason if it surfaces as
            # confusing.
            continue
        seen_aliases.add(alias)
        order_by_resolved.append(
            ResolvedOrderBy(
                expression=alias,
                direction=requested.direction,
                auto_appended_tie_break=False,
            )
        )
    # Tie-break: when caller asked for ORDER BY AND group_by has at
    # least one column not already in seen_aliases, append the first
    # unseen group_col alias as ASC. This guarantees deterministic row
    # order across runs without changing the result set. Skipping the
    # tie-break when no group_by exists (single-row aggregate) or when
    # caller didn't ask for ORDER BY at all (caller is explicitly OK
    # with database-default order) preserves the no-surprise contract.
    if order_by_resolved and group_by_columns_ordered:
        for tiebreak_index in range(len(group_by_columns_ordered)):
            tiebreak_alias = f"group_col_{tiebreak_index}"
            if tiebreak_alias not in seen_aliases:
                order_by_resolved.append(
                    ResolvedOrderBy(
                        expression=tiebreak_alias,
                        direction="asc",
                        auto_appended_tie_break=True,
                    )
                )
                break

    # Time-dimension inheritance: when the metric has no time_dimension
    # of its own AND the caller passed `time_grain`, BFS the canonical-
    # join graph for reachable entities with timestamp columns (over
    # non-fan-out edges only). 1 candidate → inherit; the inherited
    # dimension supports the full grain set so v1.2 callers can bucket
    # without the metric author having to pre-declare. 2+ → raise
    # `AmbiguousTimeDimensionError` so the agent picks one explicitly.
    # 0 → degrade gracefully: ship the plan unbucketed with resolution
    # `unavailable`; the MCP layer surfaces this as the
    # `time_dimension_unavailable` degradation reason so the agent gets
    # a useful answer plus context rather than a hard error.
    time_dimension_resolution: Literal["local", "inherited", "unavailable"] = "local"
    inherited_time_dimension: str | None = None
    inherited_via: tuple[str, ...] = ()
    effective_time_bucket: TimeGrain | None = time_grain
    if metric.time_dimension is None and time_grain is not None:
        g = _ensure_graph()
        candidates = _find_inherited_time_dimension(
            graph=g,
            anchor=metric.entity,
            store=store,
            source_connection_id=source_connection_id,
        )
        # v1.2 caller-provided disambiguator: when 2+ candidates exist,
        # narrow to the one the caller asked for. A `time_dimension`
        # that doesn't match any candidate falls through to the
        # AmbiguousTimeDimensionError below — the message lists the
        # full valid set so the agent can re-call with a real choice.
        # Keep the original `candidates` for the error envelope so the
        # agent sees every option, not just the (empty) post-filter set.
        if time_dimension is not None and len(candidates) >= 1:
            filtered = tuple(
                c for c in candidates if c[0] == time_dimension
            )
            if len(filtered) == 1:
                candidates = filtered  # type: ignore[assignment]
        if len(candidates) > 1:
            raise AmbiguousTimeDimensionError(
                anchor_entity=metric.entity,
                candidates=tuple(candidates),
            )
        if len(candidates) == 1:
            inherited_time_dimension, inherited_via = candidates[0]
            time_dimension_resolution = "inherited"
            # Force the JOIN to the inherited entity into the plan so
            # the emitter can reference it by alias. `_resolve` does
            # column-existence validation + populates `resolved_joins`
            # as a side effect — we discard the returned ResolvedColumn
            # because we only need the join materialised.
            _resolve(inherited_time_dimension, kind="group_by")
            # `_resolve` may have grown `resolved_joins`; rebuild the
            # ordered tuple so the plan ships every required join in
            # topological order.
            ordered_joins = tuple(resolved_joins.values())
        else:
            time_dimension_resolution = "unavailable"
            effective_time_bucket = None

    return MetricPlan(
        metric=metric,
        anchor_table=anchor_table,
        anchor_alias=anchor_alias,
        group_by_columns=group_by_columns_ordered,
        time_bucket=effective_time_bucket,
        filter_predicates=tuple(filter_predicates),
        limit=limit,
        joins=ordered_joins,
        order_by_clauses=tuple(order_by_resolved),
        time_dimension_resolution=time_dimension_resolution,
        inherited_time_dimension=inherited_time_dimension,
        time_dimension_inherited_via=inherited_via,
    )


# ----- canonical-join graph + BFS --------------------------------------------


class _ChainEdge:
    """One hop along a resolved chain.

    Carries the canonical join (provenance) plus the on-pair
    orientation for THIS hop's chain direction. When BFS traverses a
    canonical join in the reverse of its stored direction, the
    on_pairs are SWAPPED at edge-construction time so emit can use
    them verbatim — the emitter does not need to know about direction.

    `is_reverse_traversal` is the load-bearing direction signal for
    downstream consumers (fan-out detector, cardinality reporter):
    True when BFS walked the canonical join from its stored
    `target_entity` toward its stored `source_entity` (i.e. reverse
    of how the operator confirmed it). The earlier
    `on_pairs != join.on` heuristic gave a false NEGATIVE whenever
    the join's source and target columns shared a name (e.g. an FK
    join on `customer_id` between `rental.customer_id` and
    `customer.customer_id`) — the swapped pairs compared equal to
    the stored pairs and the flip silently never fired. This explicit
    boolean replaces that comparison.
    """

    __slots__ = (
        "is_reverse_traversal",
        "join",
        "on_pairs_in_chain_direction",
        "target_entity_in_chain",
    )

    def __init__(
        self,
        join: CanonicalJoin,
        target_entity_in_chain: str,
        on_pairs_in_chain_direction: tuple[JoinColumnPair, ...],
        is_reverse_traversal: bool,
    ) -> None:
        self.join = join
        self.target_entity_in_chain = target_entity_in_chain
        self.on_pairs_in_chain_direction = on_pairs_in_chain_direction
        self.is_reverse_traversal = is_reverse_traversal


# A directed-from-traversal-perspective adjacency map: at each entity
# node, the list of `(neighbor_entity, canonical_join, on_pairs_oriented,
# is_reverse_traversal)` tuples representing every way to leave that
# node along a canonical join. Each stored canonical join contributes
# TWO entries (one per endpoint) since BFS over canonical joins is
# undirected. The 4-tuple's last element is True when the entry walks
# the join in the reverse of its stored direction; downstream
# cardinality logic uses this rather than comparing on-pair tuples
# (the earlier on-pair heuristic broke on same-column-name FK joins).
_JoinGraph = dict[
    str,
    list[tuple[str, CanonicalJoin, tuple[JoinColumnPair, ...], bool]],
]


def _build_join_graph(edges: list[CanonicalJoin]) -> _JoinGraph:
    graph: _JoinGraph = {}
    for join in edges:
        # Stored orientation: source_entity → target_entity along on_pairs.
        # `is_reverse_traversal=False` — this edge walks the join in the
        # direction it was stored.
        graph.setdefault(join.source_entity, []).append(
            (join.target_entity, join, tuple(join.on), False)
        )
        # Reverse orientation: traversing from target_entity, the
        # on-pair columns flip so emit can produce
        # `{neighbor_alias}.{source_column} = {origin_alias}.{target_column}`
        # using the same emit logic verbatim. `dataclasses.replace`
        # ensures any future fields added to `JoinColumnPair` (e.g.
        # cast_type, nullable_override) carry over to the reverse-
        # direction edge automatically instead of being silently
        # dropped by a positional constructor.
        # `is_reverse_traversal=True` — flag this edge as walking the
        # join from its stored target toward its stored source so the
        # cardinality flip fires deterministically (without depending
        # on whether the on-pair columns happen to share names).
        swapped = tuple(
            dataclasses.replace(
                p,
                source_column=p.target_column,
                target_column=p.source_column,
            )
            for p in join.on
        )
        graph.setdefault(join.target_entity, []).append((join.source_entity, join, swapped, True))
    # Deterministic neighbor order so BFS path ties resolve identically
    # across runs — sort by canonical-join name within each adjacency
    # list.
    for neighbors in graph.values():
        neighbors.sort(key=lambda entry: entry[1].name)
    return graph


_StructuralPath = tuple[tuple[str, str], ...]


def _find_canonical_chain(
    *,
    graph: _JoinGraph,
    anchor: str,
    target: str,
    via: frozenset[str],
) -> tuple[list[tuple[str, _ChainEdge]], frozenset[str]]:
    """BFS from `anchor` to `target` over the canonical-join graph.

    Two-pass design:

      Pass 1 — structural BFS over entity pairs. Parallel canonical
        joins between the same entity pair (billing vs shipping address
        on `order → address`) are collapsed into a single edge. This
        pass identifies structural ambiguity (e.g. diamond paths via
        distinct intermediate entities) without conflating it with
        single-edge ambiguity inside an otherwise-unique chain. Tangent
        parallel canonicals — parallel canonicals on a hop that doesn't
        lie on any structural path from anchor to target — are inert:
        they don't block resolution, because the resolver doesn't need
        to traverse that edge to reach the target.

      Pass 2 — canonical-join resolution per hop on the unique
        structural path that survives via= filtering. Parallel
        canonicals on a hop the chain actually traverses, without `via`
        disambiguation, surface `AmbiguousJoinError` (preserving the v1
        single-hop contract for the on-chain case).

    Returns `(chain, consumed_via)` where `chain` is the ordered list
    of (predecessor_entity, ChainEdge) tuples and `consumed_via` is the
    subset of the input `via` set whose names constrained an actual
    hop in the resolved chain. The return-value contract (rather than
    mutating a closure-captured set) ensures consumed names are only
    observable on the success path: if BFS raises, no partial-chain
    names leak back into the caller's request-level tracking set.

    Refusal behavior:
      - No structural path from anchor to target → `UnreachableEntityError`.
      - 2+ structural paths and via= doesn't narrow to one → `AmbiguousPathError`.
      - Via= excludes every structural path → `UnknownViaJoinError`.
      - 1 structural path with parallel canonicals on a hop and via=
        doesn't pick exactly one of them → `AmbiguousJoinError`.
    """
    if anchor not in graph or target not in graph:
        # If EITHER endpoint has no edges in the canonical-join graph,
        # no path exists. An earlier guard used `and`, which was a
        # strict subset of correct unreachability: an anchor with no
        # edges still triggers an empty BFS that returns the same
        # UnreachableEntityError downstream, but the early-out is more
        # honest about what we know up-front.
        raise UnreachableEntityError(anchor_entity=anchor, target_entity=target)

    # Pass 1 — structural BFS over entity pairs.
    structural_paths = _structural_shortest_paths(graph=graph, anchor=anchor, target=target)
    if not structural_paths:
        raise UnreachableEntityError(anchor_entity=anchor, target_entity=target)

    feasible_paths = _filter_structural_paths_by_via(paths=structural_paths, graph=graph, via=via)
    if not feasible_paths:
        # Via excluded every structural path. The set of canonical-join
        # names the agent SHOULD have picked from is the union across
        # all candidate structural paths.
        available = tuple(
            sorted(
                {
                    join.name
                    for path in structural_paths
                    for join in _canonicals_on_structural_path(path=path, graph=graph)
                }
            )
        )
        raise UnknownViaJoinError(
            anchor_entity=anchor,
            target_entity=target,
            requested_via=tuple(sorted(via)),
            available_join_names=available,
        )
    if len(feasible_paths) > 1:
        candidate_paths = tuple(
            _render_structural_path_as_canonical_sequence(path=path, graph=graph)
            for path in feasible_paths
        )
        raise AmbiguousPathError(
            anchor_entity=anchor,
            target_entity=target,
            candidate_paths=candidate_paths,
        )

    # Pass 2 — resolve canonical join per hop on the unique structural
    # path. Parallel canonicals on a traversed hop without via=
    # disambiguation surface `AmbiguousJoinError` here (rather than
    # eagerly during pass 1, which is what mis-fired on tangent
    # parallels before this rewrite).
    chain: list[tuple[str, _ChainEdge]] = []
    consumed_via: set[str] = set()
    chosen_path = feasible_paths[0]
    for predecessor, neighbor in chosen_path:
        candidates = [
            (j, op, rev) for (n, j, op, rev) in graph.get(predecessor, []) if n == neighbor
        ]
        if not candidates:  # pragma: no cover — defensive
            # Pass-1/pass-2 invariant violation: pass 1 only emits a
            # hop `(predecessor, neighbor)` when `neighbor` appears in
            # `graph[predecessor]`. Unreachable on a single-request
            # graph (the graph is built once and not mutated between
            # passes), but a future refactor that filters or rebuilds
            # the graph between passes would silently land here and
            # crash later in `AmbiguousJoinError.__post_init__` when
            # it indexed into an empty `candidate_join_names`. Make
            # the invariant explicit so the failure mode is named.
            raise RuntimeError(
                f"compiler invariant violation: structural path hop "
                f"({predecessor!r}, {neighbor!r}) resolved to zero "
                f"canonical joins in pass 2 — graph-path consistency "
                f"is broken. This is a compiler bug."
            )
        if len(candidates) == 1:
            chosen_join, chosen_pairs, chosen_reverse = candidates[0]
            # Even on unique-canonical hops, record consumption: the
            # via= name may have been the lever that narrowed pass 1
            # structural ambiguity, in which case it constrained THIS
            # path even though pass 2 had no parallel-canonical choice.
            if chosen_join.name in via:
                consumed_via.add(chosen_join.name)
        else:
            matching = [(j, op, rev) for (j, op, rev) in candidates if j.name in via]
            if len(matching) == 0:
                raise AmbiguousJoinError(
                    anchor_entity=predecessor,
                    target_entity=neighbor,
                    candidate_join_names=tuple(sorted(j.name for (j, _op, _rev) in candidates)),
                )
            if len(matching) > 1:
                # via= matched 2+ parallels at this hop — caller is
                # ambiguous in their own constraint. Surface as
                # AmbiguousJoinError so the recovery shape stays
                # single-edge-shaped.
                raise AmbiguousJoinError(
                    anchor_entity=predecessor,
                    target_entity=neighbor,
                    candidate_join_names=tuple(sorted(j.name for (j, _op, _rev) in matching)),
                )
            chosen_join, chosen_pairs, chosen_reverse = matching[0]
            consumed_via.add(chosen_join.name)
        edge = _ChainEdge(
            join=chosen_join,
            target_entity_in_chain=neighbor,
            on_pairs_in_chain_direction=chosen_pairs,
            is_reverse_traversal=chosen_reverse,
        )
        chain.append((predecessor, edge))
    return chain, frozenset(consumed_via)


def _structural_shortest_paths(
    *, graph: _JoinGraph, anchor: str, target: str
) -> list[_StructuralPath]:
    """BFS over entity pairs (collapsing parallel canonicals between
    the same pair into a single edge). Returns all shortest structural
    paths from `anchor` to `target`. Empty list if no path within the
    hop cap.

    Safety cap matches the `suggest_joins` MCP tool default of 6 — a
    corrupted store could in theory present a densely connected
    adversarial graph; the cap bounds work even if it does.
    """
    max_hops = 6
    frontier: list[tuple[str, _StructuralPath]] = [(anchor, ())]
    visited: set[str] = {anchor}
    found_paths: list[_StructuralPath] = []
    for _hop in range(max_hops):
        if not frontier:
            break
        next_frontier: list[tuple[str, _StructuralPath]] = []
        # Newly-visited at this layer; promote to `visited` only after
        # the layer completes, so multiple shortest paths to the same
        # node CAN coexist at the same depth (diamond ambiguity).
        layer_visited: set[str] = set()
        for entity, path in frontier:
            # Collapse parallel canonicals on each (entity, neighbor)
            # pair into a single entity-pair edge for this pass.
            neighbor_entities = {n for (n, _j, _op, _rev) in graph.get(entity, [])}
            for neighbor in sorted(neighbor_entities):
                if neighbor in visited:
                    continue
                new_path = (*path, (entity, neighbor))
                if neighbor == target:
                    found_paths.append(new_path)
                    continue
                layer_visited.add(neighbor)
                next_frontier.append((neighbor, new_path))
        if found_paths:
            return found_paths
        visited.update(layer_visited)
        frontier = next_frontier
    return found_paths


def _canonicals_on_structural_path(
    *, path: _StructuralPath, graph: _JoinGraph
) -> list[CanonicalJoin]:
    """All canonical joins lying on any hop of `path`. Used for via=
    feasibility checking and `UnknownViaJoinError.available_join_names`.
    """
    out: list[CanonicalJoin] = []
    for predecessor, neighbor in path:
        for n, join, _op, _rev in graph.get(predecessor, []):
            if n == neighbor:
                out.append(join)
    return out


def _filter_structural_paths_by_via(
    *,
    paths: list[_StructuralPath],
    graph: _JoinGraph,
    via: frozenset[str],
) -> list[_StructuralPath]:
    """A structural path is via-feasible iff every via= name appears
    on at least one canonical join lying on the path. A via= name
    that names a real canonical join but doesn't lie on any candidate
    path filters every path out — that case raises
    `UnknownViaJoinError` upstream.
    """
    if not via:
        return paths
    return [
        path
        for path in paths
        if via.issubset({j.name for j in _canonicals_on_structural_path(path=path, graph=graph)})
    ]


def _render_structural_path_as_canonical_sequence(
    *, path: _StructuralPath, graph: _JoinGraph
) -> tuple[str, ...]:
    """Render a structural path as a canonical-join-name sequence for
    `AmbiguousPathError.candidate_paths`. Each hop picks the first
    canonical-join name alphabetically — deterministic so tests can
    assert on the output. In the typical case (a hop has exactly one
    canonical), the choice is trivial; the alphabetical rule only
    matters when a structural path passes through a hop with parallel
    canonicals AND another structural path also reaches the target,
    which is an unusual schema shape but rendered deterministically.
    """
    names: list[str] = []
    for predecessor, neighbor in path:
        canonicals_on_hop = sorted(
            j.name for (n, j, _op, _rev) in graph.get(predecessor, []) if n == neighbor
        )
        if not canonicals_on_hop:  # pragma: no cover — defensive
            # Same invariant as pass 2's empty-candidates guard:
            # `_structural_shortest_paths` only emits a hop where the
            # graph has at least one edge in that direction. Unreachable
            # on a single-request graph, but a bare `IndexError` would
            # become an opaque `internal_error` envelope downstream —
            # name the invariant so a future graph-mutation refactor
            # fails loudly here instead.
            raise RuntimeError(
                f"compiler invariant violation: structural path hop "
                f"({predecessor!r}, {neighbor!r}) has zero canonical "
                f"joins while rendering AmbiguousPathError candidate. "
                f"This is a compiler bug."
            )
        names.append(canonicals_on_hop[0])
    return tuple(names)


# ----- helpers (unchanged from v1) -------------------------------------------


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
                f"filter[{index}] operator {op!r} is unary; value must be omitted (got {value!r})"
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


def _lookup_anchor_table(store: Store, entity_name: str, source_connection_id: str) -> str:
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


def _lookup_optional_table(store: Store, entity_name: str, source_connection_id: str) -> str | None:
    """Look up a potentially-missing entity, used for UnknownColumnError
    vs UnreachableEntityError disambiguation."""
    entity = store.get_entity(entity_name, source_connection_id=source_connection_id)
    if entity is None:
        return None
    return entity.qualified_table


# ----- time-dimension inheritance --------------------------------------------
#
# Heuristic detection of columns the agent is likely to mean by "time".
# Substring match against the lowercased column data type so dialect
# variants (`timestamp`, `timestamp with time zone`, `timestamptz`,
# `datetime`, `date`) all qualify. False positives are bounded: a
# column typed `text` containing ISO dates would not be picked, which
# is the correct conservative behaviour. False negatives (a custom
# domain type that wraps a timestamp) require the metric author to
# declare `time_dimension` explicitly — the agent gets a helpful
# `AmbiguousTimeDimensionError` candidate list to copy from when the
# heuristic fires.
_TIMESTAMP_DATA_TYPE_KEYWORDS: tuple[str, ...] = (
    "timestamp",
    "datetime",
    "date",
)


def _is_timestamp_type(data_type: str) -> bool:
    """True when `data_type` names a temporal column type by substring."""
    lower = data_type.lower()
    return any(kw in lower for kw in _TIMESTAMP_DATA_TYPE_KEYWORDS)


def _find_inherited_time_dimension(
    *,
    graph: _JoinGraph,
    anchor: str,
    store: Store,
    source_connection_id: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return candidate (`<entity>.<column>`, join-chain) pairs the
    resolver could use as an inherited time_dimension.

    Walks the canonical-join graph from `anchor` over edges whose
    effective cardinality in the chain direction is `many_to_one` or
    `one_to_one` — these are NON-fan-out joins, so bucketing on the
    target's timestamp won't inflate aggregates. Edges with unknown
    cardinality are skipped (defensive: prefer to ask the agent than
    silently over-count). The anchor's own table is excluded — when
    the anchor has a timestamp column, the metric author should have
    declared `time_dimension` directly.

    The returned ordering is BFS traversal order: shortest chains first,
    alphabetical tiebreak by canonical-join name (inherited from the
    pre-sorted adjacency list in `_build_join_graph`).
    """
    candidates: list[tuple[str, tuple[str, ...]]] = []
    # `visited` maps entity_name → shortest path of join names from anchor.
    visited: dict[str, tuple[str, ...]] = {anchor: ()}
    frontier: deque[str] = deque([anchor])
    while frontier:
        current = frontier.popleft()
        current_path = visited[current]
        for neighbor, join, _on_pairs, _rev in graph.get(current, []):
            if neighbor in visited:
                continue
            # Direction-aware cardinality: when the canonical join is
            # traversed from `current` toward `neighbor` and stored
            # source == current, the stored cardinality applies as-is.
            # Otherwise we're walking it in reverse and need to flip.
            if join.source_entity == current:
                effective = join.cardinality
            else:
                effective = flip_cardinality(join.cardinality)
            # Only follow paths that don't fan out — many_to_one and
            # one_to_one preserve anchor row count; many_to_many and
            # one_to_many (in chain direction) would multiply rows
            # before bucketing, producing inflated sums.
            if effective not in ("many_to_one", "one_to_one"):
                continue
            new_path = (*current_path, join.name)
            visited[neighbor] = new_path
            frontier.append(neighbor)
            # Inspect the reached entity's bound table for timestamp
            # columns. The entity row is guaranteed to exist because
            # the canonical join's FK to `entities` covers both ends.
            entity = store.get_entity(neighbor, source_connection_id=source_connection_id)
            if entity is None:  # pragma: no cover — FK guarantee
                continue
            schema_name, table_name = entity.qualified_table.split(".", 1)
            table = store.get_table(
                schema_name=schema_name,
                name=table_name,
                source_connection_id=source_connection_id,
            )
            if table is None:  # pragma: no cover — store corruption guard
                continue
            for col in table.columns:
                if _is_timestamp_type(col.data_type):
                    candidates.append((f"{neighbor}.{col.name}", new_path))
    return candidates
