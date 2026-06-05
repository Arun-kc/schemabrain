"""Tests for the v15 graph projection builder (PR-16, ADR 0010).

`rebuild_graph_projection` reads the current entities / joins / PII tags /
row-count estimates and persists a `graph_nodes` + `graph_edges` read-model:

  - one node per entity (group passthrough, catastrophic snapshot, row_count)
  - one edge per canonical join (honest edge_origin from inference_method)
  - canonical_path_rank = 1 on the diameter spine, 0 elsewhere
  - catastrophic snapshot uses the SAME floor predicate as the PII matrix
  - idempotent (re-projection never duplicates) and empty-source safe
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.semantic.graph_projection import rebuild_graph_projection

SOURCE = "src_a"


def _table(name: str, *extra_cols: str) -> Table:
    cols = [
        Column(
            name="id",
            table_name=name,
            schema_name="public",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        )
    ]
    for i, col in enumerate(extra_cols, start=2):
        cols.append(
            Column(
                name=col,
                table_name=name,
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=i,
                is_primary_key=False,
            )
        )
    return Table(name=name, schema_name="public", columns=tuple(cols))


def _entity(name: str, table: str, group: str) -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=f"public.{table}"),
        identity="id",
        group=group,  # type: ignore[arg-type]
    )


def _join(name: str, src: str, tgt: str, inference_method: str) -> CanonicalJoin:
    return CanonicalJoin(
        name=name,
        description="",
        source_entity=src,
        target_entity=tgt,
        on=(JoinColumnPair(source_column="id", target_column="id"),),
        cardinality="many_to_one",
        inference_method=inference_method,  # type: ignore[arg-type]
    )


def _seed_saas(store: SQLiteStore) -> None:
    # tables (orders carries a catastrophic payment_card column)
    store.write_table(_table("order_items"), source_connection_id=SOURCE)  # no row estimate
    store.write_table(
        _table("orders", "card_number"), source_connection_id=SOURCE, estimated_row_count=5000
    )
    store.write_table(
        _table("users", "email"), source_connection_id=SOURCE, estimated_row_count=1000
    )
    store.write_table(_table("tenants"), source_connection_id=SOURCE, estimated_row_count=10)
    store.write_table(_table("payments"), source_connection_id=SOURCE, estimated_row_count=4000)

    # entities
    store.write_entity(
        _entity("order_item", "order_items", "activity"), source_connection_id=SOURCE
    )
    store.write_entity(_entity("order", "orders", "activity"), source_connection_id=SOURCE)
    store.write_entity(_entity("user", "users", "identity"), source_connection_id=SOURCE)
    store.write_entity(_entity("tenant", "tenants", "identity"), source_connection_id=SOURCE)
    store.write_entity(_entity("payment", "payments", "billing"), source_connection_id=SOURCE)

    # PII: card_number on orders is catastrophic (payment_card); email is plain pii.
    store.write_column_pii_tags(
        source_connection_id=SOURCE,
        qualified_table="public.orders",
        tags={"card_number": ("pii", frozenset({"payment_card"}))},
    )
    store.write_column_pii_tags(
        source_connection_id=SOURCE,
        qualified_table="public.users",
        tags={"email": ("pii", frozenset({"contact"}))},
    )

    # joins: spine order_item→order→user→tenant (3 hops) + a side branch order→payment.
    store.write_canonical_join(
        _join("order_item_to_order", "order_item", "order", "fk_constraint"),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        _join("order_to_user", "order", "user", "observed_in_query_log"),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        _join("user_to_tenant", "user", "tenant", "llm_suggested"), source_connection_id=SOURCE
    )
    store.write_canonical_join(
        _join("order_to_payment", "order", "payment", "fk_constraint"), source_connection_id=SOURCE
    )


class TestRebuildGraphProjection:
    def test_nodes_carry_group_catastrophic_rowcount(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_saas(store)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            nodes = {n.entity_name: n for n in store.list_graph_nodes(source_connection_id=SOURCE)}

            assert set(nodes) == {"order_item", "order", "user", "tenant", "payment"}
            assert nodes["order"].is_catastrophic is True  # payment_card column
            assert nodes["user"].is_catastrophic is False  # contact is not a floor cat
            assert nodes["order"].group == "activity"
            assert nodes["payment"].group == "billing"
            assert nodes["order"].row_count == 5000
            assert nodes["order_item"].row_count is None  # never fabricated 0

    def test_edges_map_inference_method_to_honest_origin(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_saas(store)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            edges = {e.join_name: e for e in store.list_graph_edges(source_connection_id=SOURCE)}

            assert edges["order_item_to_order"].edge_origin == "declared"  # fk_constraint
            assert edges["order_to_user"].edge_origin == "log_mined"  # observed_in_query_log
            assert edges["user_to_tenant"].edge_origin == "inferred"  # llm_suggested

    def test_canonical_path_rank_marks_the_diameter_spine(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_saas(store)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            ranks = {
                e.join_name: e.canonical_path_rank
                for e in store.list_graph_edges(source_connection_id=SOURCE)
            }

            # spine = order_item → order → user → tenant
            assert ranks["order_item_to_order"] == 1
            assert ranks["order_to_user"] == 1
            assert ranks["user_to_tenant"] == 1
            # side branch is off the canonical path
            assert ranks["order_to_payment"] == 0

    def test_is_idempotent(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_saas(store)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            assert len(store.list_graph_nodes(source_connection_id=SOURCE)) == 5
            assert len(store.list_graph_edges(source_connection_id=SOURCE)) == 4

    def test_empty_source_yields_empty_projection(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            assert store.list_graph_nodes(source_connection_id=SOURCE) == []
            assert store.list_graph_edges(source_connection_id=SOURCE) == []

    def test_reprojection_reflects_removed_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_saas(store)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            # Drop the side branch table → cascades the entity + its join.
            store.delete_table("public", "payments", source_connection_id=SOURCE)
            rebuild_graph_projection(store, source_connection_id=SOURCE)
            edges = {e.join_name for e in store.list_graph_edges(source_connection_id=SOURCE)}
            assert "order_to_payment" not in edges
            assert "payment" not in {
                n.entity_name for n in store.list_graph_nodes(source_connection_id=SOURCE)
            }
