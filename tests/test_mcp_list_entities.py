"""Tests for the `list_entities` MCP tool.

Two layers tested here:

  - `list_entities_impl` (pure function, store + source_connection_id in,
    `list[EntitySummary]` out) — empty, single, multi, source-filter.
  - The wired tool on the FastMCP server — envelope round-trip,
    empty/success status mapping, follow-up hints to `describe_entity`.

`list_entities` takes no arguments at v1 — the v1-scale entity count
(dozens at most) makes pagination unnecessary, and a `limit` arg would
be a charter event we'd want to deliberate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import ToolResponse
from schemabrain.mcp.list_entities import list_entities_impl
from schemabrain.mcp.shapes import EntitySummary

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
        ),
    )


def _entity(name: str, qualified_table: str, *, origin: str = "manual") -> Entity:
    return Entity(
        name=name,
        description=f"A {name} entity",
        binding=SingleTableBinding(qualified_table=qualified_table),
        identity="id",
        origin=origin,  # type: ignore[arg-type]
    )


class _StubEmbedder:
    """Minimal Embedder stub for the server wiring tests."""

    model_name = "test"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


def _build_test_server(tmp_path: Path) -> tuple[object, SQLiteStore]:
    """Build a server backed by a fresh store with the two ecommerce
    tables seeded. Tests inject entities directly via `store.write_entity`.
    """
    store = SQLiteStore(tmp_path / "s.db")
    store.write_table(_users_table(), source_connection_id="sid")
    store.write_table(_orders_table(), source_connection_id="sid")
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
            result = list_entities_impl(store=store, source_connection_id="sid")
        assert result == []

    def test_single_entity_returns_summary(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(
                _entity("customer", "public.users"),
                source_connection_id="sid",
            )
            result = list_entities_impl(store=store, source_connection_id="sid")
        assert len(result) == 1
        assert isinstance(result[0], EntitySummary)
        assert result[0].name == "customer"
        assert result[0].description == "A customer entity"
        assert result[0].qualified_table == "public.users"
        assert result[0].identity == "id"
        assert result[0].origin == "manual"

    def test_multiple_entities_alphabetical(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_table(_orders_table(), source_connection_id="sid")
            # Insert in non-alphabetical order to verify ordering.
            store.write_entity(_entity("order", "public.orders"), source_connection_id="sid")
            store.write_entity(_entity("customer", "public.users"), source_connection_id="sid")
            result = list_entities_impl(store=store, source_connection_id="sid")
        assert [e.name for e in result] == ["customer", "order"]

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_origin_field_propagates(self, tmp_path: Path, origin: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid")
            store.write_entity(
                _entity("customer", "public.users", origin=origin),
                source_connection_id="sid",
            )
            result = list_entities_impl(store=store, source_connection_id="sid")
        assert result[0].origin == origin


class TestImplSourceFilter:
    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        """Entities under one source are invisible to another."""
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_users_table(), source_connection_id="sid_a")
            store.write_table(_users_table(), source_connection_id="sid_b")
            store.write_entity(
                _entity("customer", "public.users"),
                source_connection_id="sid_a",
            )
            result_a = list_entities_impl(store=store, source_connection_id="sid_a")
            result_b = list_entities_impl(store=store, source_connection_id="sid_b")
        assert len(result_a) == 1
        assert result_b == []


# ----- envelope round-trip tests ---------------------------------------------


class TestEnvelopeEmpty:
    def test_empty_store_yields_empty_status(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(server.call_tool("list_entities", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "empty"
        assert envelope.error is None
        assert envelope.data == []
        # Empty envelope still routes the agent forward — pointing
        # back at `find_relevant_tables` lets them discover physical
        # tables before defining entities.
        assert "find_relevant_tables" in (envelope.follow_up_hints or ())


class TestEnvelopeSuccess:
    def test_returns_success_envelope_with_entities(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_entity(
                _entity("customer", "public.users"),
                source_connection_id="sid",
            )
            store.write_entity(
                _entity("order", "public.orders"),
                source_connection_id="sid",
            )
            _content, structured = asyncio.run(server.call_tool("list_entities", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "success"
        assert envelope.error is None
        assert envelope.data is not None
        assert len(envelope.data) == 2
        assert envelope.confidence == "HIGH"
        # Charter Principle 5: composition hint to drill into one entity.
        assert "describe_entity" in (envelope.follow_up_hints or ())

    def test_envelope_carries_qualified_table_form(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_entity(
                _entity("customer", "public.users"),
                source_connection_id="sid",
            )
            _content, structured = asyncio.run(server.call_tool("list_entities", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.data is not None
        # Storage splits binding into (binding_schema, binding_table);
        # the wire shape rejoins them for agent convenience.
        assert envelope.data[0]["qualified_table"] == "public.users"
