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
from typing import Any

from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, TimeGrain
from schemabrain.core.store_protocol import Store
from schemabrain.semantic.compiler.plan import (
    AmbiguousJoinError,
    AmbiguousPathError,
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
    UnknownViaJoinError,
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
    limit: int = 1000,
    via: tuple[str, ...] = (),
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

    def _ensure_graph() -> _JoinGraph:
        nonlocal graph
        if graph is None:
            edges = store.list_canonical_joins(source_connection_id=source_connection_id)
            graph = _build_join_graph(edges)
        return graph

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
            resolved_joins[edge.target_entity_in_chain] = ResolvedJoin(
                canonical_name=edge.join.name,
                source_alias=source_alias,
                target_entity=edge.target_entity_in_chain,
                target_table=target_table,
                target_alias=_alias_for(edge.target_entity_in_chain),
                on_pairs=edge.on_pairs_in_chain_direction,
                cardinality=edge.join.cardinality,
            )
        # Final ResolvedJoin for the requested entity is now cached.
        join = resolved_joins[entity_name]
        return ResolvedColumn(
            entity=entity_name,
            column=column,
            qualified_table=join.target_table,
            alias=join.target_alias,
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

    return MetricPlan(
        metric=metric,
        anchor_table=anchor_table,
        anchor_alias=anchor_alias,
        group_by_columns=tuple(group_by_resolved.values()),
        time_bucket=time_grain,
        filter_predicates=tuple(filter_predicates),
        limit=limit,
        joins=ordered_joins,
    )


# ----- canonical-join graph + BFS --------------------------------------------


class _ChainEdge:
    """One hop along a resolved chain.

    Carries the canonical join (provenance) plus the on-pair
    orientation for THIS hop's chain direction. When BFS traverses a
    canonical join in the reverse of its stored direction, the
    on_pairs are SWAPPED at edge-construction time so emit can use
    them verbatim — the emitter does not need to know about direction.
    """

    __slots__ = ("join", "on_pairs_in_chain_direction", "target_entity_in_chain")

    def __init__(
        self,
        join: CanonicalJoin,
        target_entity_in_chain: str,
        on_pairs_in_chain_direction: tuple[JoinColumnPair, ...],
    ) -> None:
        self.join = join
        self.target_entity_in_chain = target_entity_in_chain
        self.on_pairs_in_chain_direction = on_pairs_in_chain_direction


# A directed-from-traversal-perspective adjacency map: at each entity
# node, the list of (neighbor_entity, canonical_join, on_pairs_oriented)
# triples representing every way to leave that node along a canonical
# join. Each stored canonical join contributes TWO entries (one per
# endpoint) since BFS over canonical joins is undirected.
_JoinGraph = dict[str, list[tuple[str, CanonicalJoin, tuple[JoinColumnPair, ...]]]]


def _build_join_graph(edges: list[CanonicalJoin]) -> _JoinGraph:
    graph: _JoinGraph = {}
    for join in edges:
        # Stored orientation: source_entity → target_entity along on_pairs.
        graph.setdefault(join.source_entity, []).append((join.target_entity, join, tuple(join.on)))
        # Reverse orientation: traversing from target_entity, the
        # on-pair columns flip so emit can produce
        # `{neighbor_alias}.{source_column} = {origin_alias}.{target_column}`
        # using the same emit logic verbatim. `dataclasses.replace`
        # ensures any future fields added to `JoinColumnPair` (e.g.
        # cast_type, nullable_override) carry over to the reverse-
        # direction edge automatically instead of being silently
        # dropped by a positional constructor.
        swapped = tuple(
            dataclasses.replace(
                p,
                source_column=p.target_column,
                target_column=p.source_column,
            )
            for p in join.on
        )
        graph.setdefault(join.target_entity, []).append((join.source_entity, join, swapped))
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
    if anchor not in graph and target not in graph:
        # Graph has no edges at all OR neither endpoint touches the
        # graph. Either way: no path exists.
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
                    for join in _canonicals_on_structural_path(path, graph)
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
            _render_structural_path_as_canonical_sequence(path, graph) for path in feasible_paths
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
        candidates = [(j, op) for (n, j, op) in graph.get(predecessor, []) if n == neighbor]
        if len(candidates) == 1:
            chosen_join, chosen_pairs = candidates[0]
            # Even on unique-canonical hops, record consumption: the
            # via= name may have been the lever that narrowed pass 1
            # structural ambiguity, in which case it constrained THIS
            # path even though pass 2 had no parallel-canonical choice.
            if chosen_join.name in via:
                consumed_via.add(chosen_join.name)
        else:
            matching = [(j, op) for (j, op) in candidates if j.name in via]
            if len(matching) == 0:
                raise AmbiguousJoinError(
                    anchor_entity=predecessor,
                    target_entity=neighbor,
                    candidate_join_names=tuple(sorted(j.name for (j, _op) in candidates)),
                )
            if len(matching) > 1:
                # via= matched 2+ parallels at this hop — caller is
                # ambiguous in their own constraint. Surface as
                # AmbiguousJoinError so the recovery shape stays
                # single-edge-shaped.
                raise AmbiguousJoinError(
                    anchor_entity=predecessor,
                    target_entity=neighbor,
                    candidate_join_names=tuple(sorted(j.name for (j, _op) in matching)),
                )
            chosen_join, chosen_pairs = matching[0]
            consumed_via.add(chosen_join.name)
        edge = _ChainEdge(
            join=chosen_join,
            target_entity_in_chain=neighbor,
            on_pairs_in_chain_direction=chosen_pairs,
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
            neighbor_entities = {n for (n, _j, _op) in graph.get(entity, [])}
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


def _canonicals_on_structural_path(path: _StructuralPath, graph: _JoinGraph) -> list[CanonicalJoin]:
    """All canonical joins lying on any hop of `path`. Used for via=
    feasibility checking and `UnknownViaJoinError.available_join_names`.
    """
    out: list[CanonicalJoin] = []
    for predecessor, neighbor in path:
        for n, join, _op in graph.get(predecessor, []):
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
        if via.issubset({j.name for j in _canonicals_on_structural_path(path, graph)})
    ]


def _render_structural_path_as_canonical_sequence(
    path: _StructuralPath, graph: _JoinGraph
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
            j.name for (n, j, _op) in graph.get(predecessor, []) if n == neighbor
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
