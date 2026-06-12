"""Rebuild the v15 knowledge-graph projection (PR-16, ADR 0010).

The projection is a denormalised, persisted read-model of `entities` +
`canonical_joins`: one node per entity, one edge per canonical join. It is
recomputed by `rebuild_graph_projection` whenever the semantic layer changes
(the CLI calls it from `index` / `entities apply` / `joins apply`) and read
flat by `GET /api/graph` — the dashboard never re-walks the FK graph per
request.

`is_catastrophic` on a node is a snapshot computed from the SAME floor
predicate the PII matrix uses (`has_catastrophic_category`); the route
overlays a live recompute on top so the served floor can never disagree with
`/pii`. `edge_origin` is the honest evidence band; the diameter spine is
marked `canonical_path_rank = 1`.
"""

from __future__ import annotations

from schemabrain.core.entity import Entity
from schemabrain.core.graph import (
    MIN_PATH_RANK,
    GraphEdge,
    GraphNode,
    edge_origin_from_inference_method,
)
from schemabrain.core.join import CanonicalJoin
from schemabrain.core.store_protocol import Store
from schemabrain.pii.categories import has_catastrophic_category
from schemabrain.semantic.compiler import longest_canonical_path

# canonical_path_rank values (the store CHECK pins 0..2). PR-16 emits only
# the primary spine (1); off-path edges keep the GraphEdge default
# (MIN_PATH_RANK = 0); alternates (2) are reserved for PR-17.
_PRIMARY_PATH_RANK = 1


def _entity_is_catastrophic(store: Store, entity: Entity, *, source_connection_id: str) -> bool:
    """Snapshot of an entity's catastrophic-floor flag from its bound table.

    Same traversal + floor predicate the PII matrix / `/api/entities` use
    (`get_table` → `get_column_pii_tags` → `has_catastrophic_category`) so
    the projection's snapshot agrees with the live surfaces.
    """
    schema_name, table_name = entity.qualified_table.split(".", 1)
    table = store.get_table(schema_name, table_name, source_connection_id=source_connection_id)
    if table is None:  # pragma: no cover - the entity→table FK guarantees it
        return False
    tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=entity.qualified_table,
        columns=[column.name for column in table.columns],
    )
    return any(has_catastrophic_category(categories) for _sensitivity, categories in tags.values())


def _graph_edge_from_join(join: CanonicalJoin, *, spine_edges: set[str]) -> GraphEdge:
    """Project one canonical join onto a graph edge.

    `cardinality` rides ONLY on `declared` (FK-backed) edges. A `log_mined`
    or `inferred` join may still carry a human- or dbt-asserted `cardinality`
    in its YAML, but the engine never validated it against a live DB
    constraint — so the projection drops it rather than render an unverified
    shape identically to an engine-derived one (ADR 0011, the same
    source-or-drop honesty line as ADR 0009/0010).
    """
    origin = edge_origin_from_inference_method(join.inference_method)
    return GraphEdge(
        join_name=join.name,
        source_entity=join.source_entity,
        target_entity=join.target_entity,
        edge_origin=origin,
        canonical_path_rank=(_PRIMARY_PATH_RANK if join.name in spine_edges else MIN_PATH_RANK),
        cardinality=join.cardinality if origin == "declared" else None,
    )


def rebuild_graph_projection(store: Store, *, source_connection_id: str) -> None:
    """Recompute and persist the graph projection for one source (idempotent).

    Reads the source's entities, canonical joins, PII tags, and row-count
    estimates; writes one `graph_nodes` row per entity and one `graph_edges`
    row per canonical join via the atomic `write_graph_projection` swap. A
    source with no entities/joins yields an empty projection.
    """
    entities = store.list_entities(source_connection_id=source_connection_id)
    joins = store.list_canonical_joins(source_connection_id=source_connection_id)
    row_counts = store.estimated_row_counts(source_connection_id=source_connection_id)

    nodes = [
        GraphNode(
            entity_name=entity.name,
            group=entity.group,
            # At-rest catastrophic snapshot. GET /api/graph recomputes this
            # LIVE per request (so the floor never disagrees with /pii), so
            # this persisted value is write-only until PR-17's server seed
            # reads it directly — do not delete it as dead (ADR 0010 §2).
            is_catastrophic=_entity_is_catastrophic(
                store, entity, source_connection_id=source_connection_id
            ),
            row_count=row_counts.get(entity.qualified_table),
        )
        for entity in entities
    ]

    spine_edges = set(longest_canonical_path(joins=joins).edges)
    edges = [_graph_edge_from_join(join, spine_edges=spine_edges) for join in joins]

    store.write_graph_projection(
        source_connection_id=source_connection_id, nodes=nodes, edges=edges
    )
