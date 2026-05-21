"""Tests for the `list_joins` MCP tool.

Mirrors `test_mcp_list_entities.py` and `test_mcp_list_metrics.py`.
Two layers:

  - `list_joins_impl` (pure function, store + source_connection_id
    in, `list[JoinSummary]` out) — empty, single, multi alphabetical,
    source filter, direction preservation.
  - The wired tool on the FastMCP server — envelope round-trip,
    empty/success status mapping, follow-up hints to `resolve_join`
    (success) and `suggest_joins` (empty).

Closes the parity gap surfaced alongside `list_metrics` in the
2026-05-21 manual smoke: five canonical joins lived in the store,
the agent had no way to enumerate them before deciding which pair
to resolve.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import ToolResponse
from schemabrain.mcp.list_joins import list_joins_impl
from schemabrain.mcp.shapes import JoinSummary

# ----- helpers ---------------------------------------------------------------


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
    )


def _orders_table() -> Table:
    return Table(
        name="orders",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            Column(
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
            ),
        ),
    )


def _user_entity() -> Entity:
    return Entity(
        name="user",
        description="A registered user",
        binding=SingleTableBinding(qualified_table="public.users"),
        identity="id",
        origin="manual",
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="A purchase transaction",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
        origin="manual",
    )


def _join(
    name: str = "orders_user_id",
    *,
    source_entity: str = "order",
    target_entity: str = "user",
    origin: str = "manual",
) -> CanonicalJoin:
    return CanonicalJoin(
        name=name,
        description=f"Canonical join {name}",
        source_entity=source_entity,
        target_entity=target_entity,
        on=(JoinColumnPair(source_column="user_id", target_column="id"),),
        origin=origin,  # type: ignore[arg-type]
    )


class _StubEmbedder:
    """Minimal Embedder stub for server wiring tests."""

    model_name = "test"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


def _build_test_server(tmp_path: Path) -> tuple[object, SQLiteStore]:
    """Build a server backed by a fresh store seeded with two
    entities + their bound tables so canonical joins can be written.
    """
    store = SQLiteStore(tmp_path / "s.db")
    store.write_table(_users_table(), source_connection_id="sid")
    store.write_table(_orders_table(), source_connection_id="sid")
    store.write_entity(_user_entity(), source_connection_id="sid")
    store.write_entity(_order_entity(), source_connection_id="sid")
    server = build_server(
        store=store,
        source_connection_id="sid",
        embedder=_StubEmbedder(),
    )
    return server, store


# ----- impl-level tests ------------------------------------------------------


class TestImplHappyPath:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            result = list_joins_impl(store=store, source_connection_id="sid")
        assert result == []

    def test_single_join_returns_summary(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_user_entity(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_canonical_join(_join(), source_connection_id="sid")
            result = list_joins_impl(store=store, source_connection_id="sid")
        assert len(result) == 1
        j = result[0]
        assert isinstance(j, JoinSummary)
        assert j.name == "orders_user_id"
        assert j.source_entity == "order"
        assert j.target_entity == "user"
        assert j.origin == "manual"

    def test_multiple_joins_alphabetical(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_user_entity(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            # Insert in non-alphabetical order to verify ordering.
            store.write_canonical_join(_join("orders_user_id"), source_connection_id="sid")
            store.write_canonical_join(
                _join(
                    "orders_billing_user_id",
                    source_entity="order",
                    target_entity="user",
                ),
                source_connection_id="sid",
            )
            result = list_joins_impl(store=store, source_connection_id="sid")
        assert [j.name for j in result] == [
            "orders_billing_user_id",
            "orders_user_id",
        ]

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_origin_field_propagates(self, tmp_path: Path, origin: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_user_entity(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_canonical_join(_join(origin=origin), source_connection_id="sid")
            result = list_joins_impl(store=store, source_connection_id="sid")
        assert result[0].origin == origin

    def test_direction_preserved(self, tmp_path: Path) -> None:
        """`source_entity` and `target_entity` reflect the stored
        confirmation direction, not alphabetical reordering."""
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_user_entity(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            # Stored as order -> user (FK direction); the summary
            # must preserve that orientation.
            store.write_canonical_join(_join(), source_connection_id="sid")
            result = list_joins_impl(store=store, source_connection_id="sid")
        assert result[0].source_entity == "order"
        assert result[0].target_entity == "user"


class TestImplSourceFilter:
    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid_a")
            store.write_table(_orders_table(), source_connection_id="sid_a")
            store.write_entity(_user_entity(), source_connection_id="sid_a")
            store.write_entity(_order_entity(), source_connection_id="sid_a")
            store.write_table(_users_table(), source_connection_id="sid_b")
            store.write_canonical_join(_join(), source_connection_id="sid_a")
            result_a = list_joins_impl(store=store, source_connection_id="sid_a")
            result_b = list_joins_impl(store=store, source_connection_id="sid_b")
        assert len(result_a) == 1
        assert result_b == []


# ----- envelope round-trip tests ---------------------------------------------


class TestEnvelopeEmpty:
    def test_empty_store_yields_empty_status(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(server.call_tool("list_joins", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "empty"
        assert envelope.error is None
        assert envelope.data == []
        # Empty envelope points the agent at `suggest_joins` so it
        # can survey FK candidates that could become canonical joins.
        assert "suggest_joins" in (envelope.follow_up_hints or ())


class TestEnvelopeSuccess:
    def test_returns_success_envelope_with_joins(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_canonical_join(_join(), source_connection_id="sid")
            _content, structured = asyncio.run(server.call_tool("list_joins", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "success"
        assert envelope.error is None
        assert envelope.data is not None
        assert len(envelope.data) == 1
        assert envelope.confidence == "HIGH"
        # Charter Principle 5: composition hint to drill into one join.
        assert "resolve_join" in (envelope.follow_up_hints or ())

    def test_envelope_carries_entity_pair(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_canonical_join(_join(), source_connection_id="sid")
            _content, structured = asyncio.run(server.call_tool("list_joins", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.data is not None
        assert envelope.data[0]["source_entity"] == "order"
        assert envelope.data[0]["target_entity"] == "user"
        assert envelope.data[0]["name"] == "orders_user_id"
