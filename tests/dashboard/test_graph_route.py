"""Tests for the GET /api/graph sidecar route (PR-16, ADR 0010).

The route serves the persisted v15 graph projection read-only: one node
per entity, one edge per canonical join (honest `evidence`), and the
ordered canonical spine. The catastrophic floor is recomputed LIVE per
node from the current PII tags, so the graph can never disagree with
/pii even after a tag change the last projection did not see.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.source_id import make_source_id
from schemabrain.core.store import SQLiteStore
from schemabrain.dashboard.sidecar import is_ui_available
from schemabrain.semantic.graph_projection import rebuild_graph_projection

pytestmark = pytest.mark.skipif(
    not is_ui_available(),
    reason="`schemabrain[ui]` extra not installed",
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

SRC = "graph-test-source"


def _table(name: str, *cols: str) -> Table:
    columns = [
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
    for i, col in enumerate(cols, start=2):
        columns.append(
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
    return Table(name=name, schema_name="public", columns=tuple(columns))


def _entity(name: str, table: str, group: str) -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=f"public.{table}"),
        identity="id",
        group=group,  # type: ignore[arg-type]
    )


def _join(name: str, src: str, tgt: str, method: str) -> CanonicalJoin:
    return CanonicalJoin(
        name=name,
        description="",
        source_entity=src,
        target_entity=tgt,
        on=(JoinColumnPair(source_column="id", target_column="id"),),
        cardinality="many_to_one",
        inference_method=method,  # type: ignore[arg-type]
    )


def _seed_graph(store_path: Path, *, source: str = SRC, with_card_tag: bool = True) -> None:
    """Spine order_item→order→user→tenant (the diameter) + a payment
    side-branch off `order` (off the canonical path), projected."""
    with SQLiteStore(store_path) as store:
        store.write_table(_table("order_items"), source_connection_id=source)
        store.write_table(
            _table("orders", "card_number"), source_connection_id=source, estimated_row_count=5000
        )
        store.write_table(_table("users"), source_connection_id=source, estimated_row_count=1000)
        store.write_table(_table("tenants"), source_connection_id=source)
        store.write_table(_table("payments"), source_connection_id=source)
        store.write_entity(
            _entity("order_item", "order_items", "activity"), source_connection_id=source
        )
        store.write_entity(_entity("order", "orders", "activity"), source_connection_id=source)
        store.write_entity(_entity("user", "users", "identity"), source_connection_id=source)
        store.write_entity(_entity("tenant", "tenants", "identity"), source_connection_id=source)
        store.write_entity(_entity("payment", "payments", "billing"), source_connection_id=source)
        if with_card_tag:
            store.write_column_pii_tags(
                source_connection_id=source,
                qualified_table="public.orders",
                tags={"card_number": ("pii", frozenset({"payment_card"}))},
            )
        store.write_canonical_join(
            _join("order_item_to_order", "order_item", "order", "fk_constraint"),
            source_connection_id=source,
        )
        store.write_canonical_join(
            _join("order_to_user", "order", "user", "fk_constraint"), source_connection_id=source
        )
        store.write_canonical_join(
            _join("user_to_tenant", "user", "tenant", "observed_in_query_log"),
            source_connection_id=source,
        )
        store.write_canonical_join(
            _join("order_to_payment", "order", "payment", "llm_suggested"),
            source_connection_id=source,
        )
        rebuild_graph_projection(store, source_connection_id=source)


def test_graph_response_shape_is_stable(client: TestClient, store_path: Path) -> None:
    _seed_graph(store_path)
    resp = client.get("/api/graph", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"source_connection_id", "nodes", "edges", "canonical_path"}
    assert set(body["nodes"][0]) == {"id", "label", "group", "catastrophic", "row_count"}
    assert set(body["edges"][0]) == {"id", "source", "target", "evidence", "canonical_path_rank"}
    assert set(body["canonical_path"]) == {"nodes", "edges", "hops"}


def test_graph_409_when_store_has_no_sources(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resp = client.get("/api/graph")
    assert resp.status_code == 409


def test_graph_empty_arrays_when_source_has_no_projection(
    client: TestClient, store_path: Path
) -> None:
    # A source exists (a table indexed) but no entities/joins → empty graph.
    with SQLiteStore(store_path) as store:
        store.write_table(_table("lonely"), source_connection_id=SRC)
    resp = client.get("/api/graph", params={"source_connection_id": SRC})
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["edges"] == []
    assert body["canonical_path"] == {"nodes": [], "edges": [], "hops": 0}


def test_graph_nodes_carry_group_and_rowcount(client: TestClient, store_path: Path) -> None:
    _seed_graph(store_path)
    nodes = {
        n["id"]: n
        for n in client.get("/api/graph", params={"source_connection_id": SRC}).json()["nodes"]
    }
    assert set(nodes) == {"order_item", "order", "user", "tenant", "payment"}
    assert nodes["order"]["group"] == "activity"
    assert nodes["payment"]["group"] == "billing"
    assert nodes["order"]["row_count"] == 5000
    assert nodes["tenant"]["row_count"] is None  # never fabricated 0


def test_graph_edges_carry_honest_evidence(client: TestClient, store_path: Path) -> None:
    _seed_graph(store_path)
    edges = {
        e["id"]: e
        for e in client.get("/api/graph", params={"source_connection_id": SRC}).json()["edges"]
    }
    assert edges["order_to_user"]["evidence"] == "declared"  # fk_constraint
    assert edges["user_to_tenant"]["evidence"] == "log_mined"  # observed_in_query_log
    assert edges["order_to_payment"]["evidence"] == "inferred"  # llm_suggested
    assert all(e["evidence"] in {"declared", "log_mined", "inferred"} for e in edges.values())
    # The side-branch is off the canonical spine.
    assert edges["order_to_payment"]["canonical_path_rank"] == 0


def test_graph_node_catastrophic_is_live(client: TestClient, store_path: Path) -> None:
    """The floor is a LIVE overlay: a payment_card tag added AFTER the
    projection was built still flags the node catastrophic (ADR 0010)."""
    _seed_graph(store_path, with_card_tag=False)  # projection snapshot = not catastrophic
    # Simulate a `policy tag override` the projection never saw.
    with SQLiteStore(store_path) as store:
        store.write_column_pii_tags(
            source_connection_id=SRC,
            qualified_table="public.orders",
            tags={"card_number": ("pii", frozenset({"payment_card"}))},
        )
    nodes = {
        n["id"]: n
        for n in client.get("/api/graph", params={"source_connection_id": SRC}).json()["nodes"]
    }
    assert nodes["order"]["catastrophic"] is True  # live, despite stale snapshot
    assert nodes["user"]["catastrophic"] is False


def test_graph_canonical_path_is_ordered(client: TestClient, store_path: Path) -> None:
    _seed_graph(store_path)
    path = client.get("/api/graph", params={"source_connection_id": SRC}).json()["canonical_path"]
    # diameter spine = order_item → order → user → tenant (4 nodes / 3 hops);
    # the payment side-branch off `order` is off the canonical path.
    assert path["nodes"] == ["order_item", "order", "user", "tenant"]
    assert path["edges"] == ["order_item_to_order", "order_to_user", "user_to_tenant"]
    assert path["hops"] == 3


def test_graph_canonical_path_ignores_rank2_alternates(
    client: TestClient, store_path: Path
) -> None:
    """The route reconstructs the primary path from rank-1 edges ONLY; a
    rank-2 alternate (reserved for PR-17) must not fold the spine into a
    cycle and blank the canonical_path (ADR 0010 §3). Written against the
    persisted projection directly since PR-16 never emits rank-2 itself."""
    from schemabrain.core.graph import GraphEdge, GraphNode

    with SQLiteStore(store_path) as store:
        for name in ("a", "b", "c", "d"):
            store.write_table(_table(name), source_connection_id=SRC)
            store.write_entity(_entity(name, name, "other"), source_connection_id=SRC)
        store.write_canonical_join(
            _join("a_to_b", "a", "b", "fk_constraint"), source_connection_id=SRC
        )
        store.write_canonical_join(
            _join("b_to_c", "b", "c", "fk_constraint"), source_connection_id=SRC
        )
        store.write_canonical_join(
            _join("c_to_d", "c", "d", "fk_constraint"), source_connection_id=SRC
        )
        store.write_canonical_join(
            _join("a_to_d", "a", "d", "fk_constraint"), source_connection_id=SRC
        )
        # Primary spine a→b→c→d (rank 1) + a rank-2 alternate a→d.
        store.write_graph_projection(
            source_connection_id=SRC,
            nodes=[GraphNode(n, group="other") for n in ("a", "b", "c", "d")],
            edges=[
                GraphEdge("a_to_b", source_entity="a", target_entity="b", canonical_path_rank=1),
                GraphEdge("b_to_c", source_entity="b", target_entity="c", canonical_path_rank=1),
                GraphEdge("c_to_d", source_entity="c", target_entity="d", canonical_path_rank=1),
                GraphEdge("a_to_d", source_entity="a", target_entity="d", canonical_path_rank=2),
            ],
        )
    path = client.get("/api/graph", params={"source_connection_id": SRC}).json()["canonical_path"]
    assert path["nodes"] == ["a", "b", "c", "d"]
    assert path["edges"] == ["a_to_b", "b_to_c", "c_to_d"]


def test_graph_catastrophic_matches_pii_matrix(client: TestClient, store_path: Path) -> None:
    """Cross-surface floor invariant (ADR 0010): every node's catastrophic
    flag from /api/graph equals the PII matrix's `has_catastrophic` for the
    same entity — the two surfaces can never disagree on the floor."""
    _seed_graph(store_path)  # `order` carries payment_card; the rest do not
    graph = client.get("/api/graph", params={"source_connection_id": SRC}).json()
    matrix = client.get("/api/entities/pii-matrix", params={"source_connection_id": SRC}).json()
    graph_catastrophic = {node["id"]: node["catastrophic"] for node in graph["nodes"]}
    matrix_catastrophic = {e["name"]: e["has_catastrophic"] for e in matrix["entities"]}
    assert graph_catastrophic == matrix_catastrophic
    assert graph_catastrophic["order"] is True  # the floor actually fires


def test_graph_never_echoes_raw_connection_url(
    client: TestClient, store_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    secret_url = "postgresql://admin:SuperSecret@db.internal:5432/prod"
    _seed_graph(store_path, source=make_source_id(secret_url))
    resp = client.get("/api/graph", params={"source_connection_id": secret_url})
    assert resp.status_code == 200
    assert "SuperSecret" not in resp.text
    assert secret_url not in resp.text
    assert resp.json()["source_connection_id"] == make_source_id(secret_url)
