"""Tests for the v15 graph projection store layer (PR-16, ADR 0010).

Locks the persisted read-model contract:

  - `write_graph_projection` is an idempotent DELETE+INSERT swap per
    source (a re-projection never duplicates rows)
  - empty inputs wipe the projection for the source
  - `list_graph_nodes` / `list_graph_edges` round-trip the rows in a
    deterministic order (entity_name / join_name)
  - `is_catastrophic` round-trips as a bool; `row_count` None survives
  - edge FK to `canonical_joins` is enforced (no orphan edges)
  - entity deletion cascades through the projection
  - source isolation: one source's projection never leaks into another
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.graph import GraphEdge, GraphNode
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

SOURCE_A = "src_a"
SOURCE_B = "src_b"


# ----- helpers ---------------------------------------------------------------


def _table(name: str) -> Table:
    return Table(
        name=name,
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name=name,
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
    )


def _entity(name: str, table: str, *, group: str = "other") -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=f"public.{table}"),
        identity="id",
        group=group,  # type: ignore[arg-type]
    )


def _join(name: str, source_entity: str, target_entity: str) -> CanonicalJoin:
    return CanonicalJoin(
        name=name,
        description="",
        source_entity=source_entity,
        target_entity=target_entity,
        on=(JoinColumnPair(source_column="id", target_column="id"),),
        cardinality="many_to_one",
        inference_method="fk_constraint",
    )


def _seed_two_entity_chain(store: SQLiteStore, source: str = SOURCE_A) -> None:
    """customer + order entities joined customer←order."""
    store.write_table(_table("users"), source_connection_id=source)
    store.write_table(_table("orders"), source_connection_id=source)
    store.write_entity(_entity("customer", "users", group="identity"), source_connection_id=source)
    store.write_entity(_entity("order", "orders", group="activity"), source_connection_id=source)
    store.write_canonical_join(
        _join("order_to_customer", "order", "customer"), source_connection_id=source
    )


# ----- round-trip ------------------------------------------------------------


class TestWriteReadRoundTrip:
    def test_nodes_and_edges_round_trip_sorted(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_two_entity_chain(store)
            store.write_graph_projection(
                source_connection_id=SOURCE_A,
                nodes=[
                    GraphNode("order", group="activity", is_catastrophic=True, row_count=42),
                    GraphNode("customer", group="identity", is_catastrophic=False, row_count=None),
                ],
                edges=[
                    GraphEdge(
                        "order_to_customer",
                        source_entity="order",
                        target_entity="customer",
                        edge_origin="declared",
                        canonical_path_rank=1,
                    )
                ],
            )

            nodes = store.list_graph_nodes(source_connection_id=SOURCE_A)
            assert [n.entity_name for n in nodes] == ["customer", "order"]  # sorted
            order_node = nodes[1]
            assert order_node.group == "activity"
            assert order_node.is_catastrophic is True
            assert order_node.row_count == 42
            assert nodes[0].row_count is None  # None survives, not 0

            edges = store.list_graph_edges(source_connection_id=SOURCE_A)
            assert len(edges) == 1
            assert edges[0].join_name == "order_to_customer"
            assert edges[0].edge_origin == "declared"
            assert edges[0].canonical_path_rank == 1

    def test_empty_projection_on_fresh_store(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            assert store.list_graph_nodes(source_connection_id=SOURCE_A) == []
            assert store.list_graph_edges(source_connection_id=SOURCE_A) == []


# ----- idempotency -----------------------------------------------------------


class TestIdempotency:
    def test_reprojection_does_not_duplicate(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_two_entity_chain(store)
            nodes = [GraphNode("customer", group="identity"), GraphNode("order")]
            edges = [
                GraphEdge("order_to_customer", source_entity="order", target_entity="customer")
            ]
            store.write_graph_projection(source_connection_id=SOURCE_A, nodes=nodes, edges=edges)
            store.write_graph_projection(source_connection_id=SOURCE_A, nodes=nodes, edges=edges)
            assert len(store.list_graph_nodes(source_connection_id=SOURCE_A)) == 2
            assert len(store.list_graph_edges(source_connection_id=SOURCE_A)) == 1

    def test_empty_inputs_wipe_the_projection(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_two_entity_chain(store)
            store.write_graph_projection(
                source_connection_id=SOURCE_A,
                nodes=[GraphNode("customer"), GraphNode("order")],
                edges=[
                    GraphEdge("order_to_customer", source_entity="order", target_entity="customer")
                ],
            )
            store.write_graph_projection(source_connection_id=SOURCE_A, nodes=[], edges=[])
            assert store.list_graph_nodes(source_connection_id=SOURCE_A) == []
            assert store.list_graph_edges(source_connection_id=SOURCE_A) == []


# ----- integrity + isolation -------------------------------------------------


class TestIntegrity:
    def test_edge_to_unknown_join_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_two_entity_chain(store)
            with pytest.raises(sqlite3.IntegrityError):
                store.write_graph_projection(
                    source_connection_id=SOURCE_A,
                    nodes=[GraphNode("customer"), GraphNode("order")],
                    edges=[
                        GraphEdge("ghost_join", source_entity="order", target_entity="customer")
                    ],
                )

    def test_entity_delete_cascades_through_projection(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_two_entity_chain(store)
            store.write_graph_projection(
                source_connection_id=SOURCE_A,
                nodes=[GraphNode("customer"), GraphNode("order")],
                edges=[
                    GraphEdge("order_to_customer", source_entity="order", target_entity="customer")
                ],
            )
            # Deleting the orders table cascades: entity `order` -> its
            # graph_node AND the edge that referenced it.
            store.delete_table("public", "orders", source_connection_id=SOURCE_A)
            remaining = {
                n.entity_name for n in store.list_graph_nodes(source_connection_id=SOURCE_A)
            }
            assert remaining == {"customer"}
            assert store.list_graph_edges(source_connection_id=SOURCE_A) == []

    def test_source_isolation(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_two_entity_chain(store, source=SOURCE_A)
            _seed_two_entity_chain(store, source=SOURCE_B)
            store.write_graph_projection(
                source_connection_id=SOURCE_A,
                nodes=[GraphNode("customer")],
                edges=[],
            )
            assert [
                n.entity_name for n in store.list_graph_nodes(source_connection_id=SOURCE_A)
            ] == ["customer"]
            assert store.list_graph_nodes(source_connection_id=SOURCE_B) == []


# ----- domain-model validation ----------------------------------------------


class TestGraphModelValidation:
    def test_bad_edge_origin_rejected(self) -> None:
        with pytest.raises(ValueError, match="edge_origin"):
            GraphEdge("j", source_entity="a", target_entity="b", edge_origin="inspected_sql")  # type: ignore[arg-type]

    def test_path_rank_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="canonical_path_rank"):
            GraphEdge("j", source_entity="a", target_entity="b", canonical_path_rank=3)

    def test_bad_group_rejected(self) -> None:
        with pytest.raises(ValueError, match="group"):
            GraphNode("e", group="bogus")  # type: ignore[arg-type]
