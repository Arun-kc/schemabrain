"""Tests for SQLiteStore entity CRUD + dbt-owned write guard.

Locks the store-side contracts:
  - `write_entity` / `get_entity` / `list_entities` round-trip
  - UPSERT semantics on `(source_connection_id, name)` PK
  - FK CASCADE: dropping a bound table sweeps its entity rows
  - Dbt-owned write guard: an entity with `origin = "dbt_import"`
    cannot be overwritten by a manual or suggested entity;
    idempotent re-imports are allowed.

The bound-table FK is enforced at the SQLite layer so an entity can
only reference an indexed physical table — this prevents a future
LLM-suggest pipeline from writing entities against tables the user
hasn't indexed yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import DbtOwnedEntityError, SQLiteStore

SOURCE_A = "src_a"
SOURCE_B = "src_b"


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
            Column(
                name="email",
                table_name="users",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
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


def _customer_entity(origin: str = "manual", description: str = "A shopper") -> Entity:
    return Entity(
        name="customer",
        description=description,
        binding=SingleTableBinding(qualified_table="public.users"),
        identity="id",
        origin=origin,  # type: ignore[arg-type]
    )


def _order_entity(origin: str = "manual") -> Entity:
    return Entity(
        name="order",
        description="One placed order",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
        origin=origin,  # type: ignore[arg-type]
    )


# ----- write + read round-trip -----------------------------------------------


class TestWriteReadRoundTrip:
    def test_write_then_get_returns_entity(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            entity = _customer_entity()
            store.write_entity(entity, source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got == entity

    def test_get_returns_none_for_unknown_name(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            got = store.get_entity("ghost", source_connection_id=SOURCE_A)
        assert got is None

    def test_get_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        """Entities under SOURCE_A are invisible to SOURCE_B and vice versa."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            assert store.get_entity("customer", source_connection_id=SOURCE_A) is not None
            assert store.get_entity("customer", source_connection_id=SOURCE_B) is None

    def test_entity_survives_reopening_the_store(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        entity = _customer_entity()
        with SQLiteStore(db_path) as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(entity, source_connection_id=SOURCE_A)
        with SQLiteStore(db_path) as store:
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got == entity

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_all_origin_literals_round_trip(self, tmp_path: Path, origin: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(origin=origin), source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == origin

    @pytest.mark.parametrize("group", ["identity", "billing", "activity", "other"])
    def test_group_round_trips_through_store(self, tmp_path: Path, group: str) -> None:
        # v15: the cosmetic `group` column survives the write_entity →
        # get_entity round-trip across every enum value (the wiring spans
        # the INSERT, the ON CONFLICT update, the SELECT, and
        # `_row_to_entity` — this pins all four in lock-step).
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            entity = Entity(
                name="customer",
                description="A shopper",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
                group=group,  # type: ignore[arg-type]
            )
            store.write_entity(entity, source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.group == group

    def test_store_does_not_enforce_group_enum_at_sql_layer(self, tmp_path: Path) -> None:
        # `group` is enforced by `Entity.__post_init__`, NOT by a SQL
        # CHECK (it's a cosmetic, growth-expected enum on a single-writer
        # cache — see core/entity.py). This pins that decision: a raw
        # INSERT bypassing `Entity` with an out-of-enum group is accepted
        # by the store. If a future change re-adds a CHECK, this fails
        # loudly and the author has to reckon with the design note.
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            conn = store._require_conn()
            conn.execute(
                "INSERT INTO entities (source_connection_id, name, description, "
                'binding_schema, binding_table, identity, origin, "group", '
                "created_at, updated_at) "
                "VALUES (?, ?, '', ?, ?, ?, 'manual', 'not_a_real_group', 0, 0)",
                (SOURCE_A, "raw_row", "public", "users", "id"),
            )
            conn.commit()
            stored_group = conn.execute(
                'SELECT "group" FROM entities WHERE name = ?', ("raw_row",)
            ).fetchone()["group"]
        # The store accepted the out-of-enum value — enforcement lives in
        # the application layer, not the database.
        assert stored_group == "not_a_real_group"


class TestBindConfidenceRationaleRoundTrip:
    """v15: the model self-rating columns survive write → read.

    Pins the four-point wiring (INSERT, ON CONFLICT update, SELECT,
    `_row_to_entity`) for `bind_confidence` + `rationale`, the same span
    the `group` round-trip test guards.
    """

    @pytest.mark.parametrize("level", ["high", "medium", "low"])
    def test_bind_confidence_round_trips(self, tmp_path: Path, level: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            entity = Entity(
                name="customer",
                description="A shopper",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
                origin="suggested",
                bind_confidence=level,  # type: ignore[arg-type]
                rationale="sole table with a customer-shaped identity",
            )
            store.write_entity(entity, source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got == entity
        assert got is not None
        assert got.bind_confidence == level
        assert got.rationale == "sole table with a customer-shaped identity"

    def test_null_confidence_and_empty_rationale_round_trip(self, tmp_path: Path) -> None:
        # Hand-authored entities never set these — NULL / '' must survive
        # so a manual entity is value-equal across the round-trip.
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            entity = _customer_entity()
            store.write_entity(entity, source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got == entity
        assert got is not None
        assert got.bind_confidence is None
        assert got.rationale == ""

    def test_upsert_overwrites_confidence_and_rationale(self, tmp_path: Path) -> None:
        # A re-suggest that lands a new rating must replace the prior one,
        # not keep a stale value — the ON CONFLICT update has to set both.
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(
                Entity(
                    name="customer",
                    description="A shopper",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="suggested",
                    bind_confidence="high",
                    rationale="first pass",
                ),
                source_connection_id=SOURCE_A,
            )
            store.write_entity(
                Entity(
                    name="customer",
                    description="A shopper",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="suggested",
                    bind_confidence="low",
                    rationale="second pass, weaker signal",
                ),
                source_connection_id=SOURCE_A,
            )
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.bind_confidence == "low"
        assert got.rationale == "second pass, weaker signal"

    def test_confidence_round_trips_through_list_entities(self, tmp_path: Path) -> None:
        # list_entities shares `_row_to_entity` but has its own SELECT —
        # pin that it hydrates the v15 columns too (the sidecar entities
        # rollup reads confidence off this path).
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(
                Entity(
                    name="customer",
                    description="A shopper",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="suggested",
                    bind_confidence="medium",
                    rationale="ok",
                ),
                source_connection_id=SOURCE_A,
            )
            listed = store.list_entities(source_connection_id=SOURCE_A)
        assert len(listed) == 1
        assert listed[0].bind_confidence == "medium"
        assert listed[0].rationale == "ok"


# ----- list_entities ---------------------------------------------------------


class TestListEntities:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.list_entities(source_connection_id=SOURCE_A) == []

    def test_lists_all_entities_for_source(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_orders_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            store.write_entity(_order_entity(), source_connection_id=SOURCE_A)
            entities = store.list_entities(source_connection_id=SOURCE_A)
        names = sorted(e.name for e in entities)
        assert names == ["customer", "order"]

    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            assert len(store.list_entities(source_connection_id=SOURCE_A)) == 1
            assert store.list_entities(source_connection_id=SOURCE_B) == []

    def test_lists_across_sources_when_no_filter(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_B)
            entities = store.list_entities()
        assert len(entities) == 2

    def test_ordering_is_deterministic_by_name(self, tmp_path: Path) -> None:
        """Callers (MCP list_entities tool) depend on stable ordering."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_orders_table(), source_connection_id=SOURCE_A)
            # Write in non-alphabetical insertion order.
            store.write_entity(_order_entity(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            entities = store.list_entities(source_connection_id=SOURCE_A)
        assert [e.name for e in entities] == ["customer", "order"]


# ----- UPSERT semantics ------------------------------------------------------


class TestUpsert:
    def test_writing_same_entity_twice_replaces_old_row(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(description="v1"), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(description="v2"), source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.description == "v2"
        # Verify there's exactly one row, not two.
        with SQLiteStore(tmp_path / "store.db") as store:
            assert len(store.list_entities(source_connection_id=SOURCE_A)) == 1

    def test_same_name_different_sources_keeps_both(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_B)
            assert store.get_entity("customer", source_connection_id=SOURCE_A) is not None
            assert store.get_entity("customer", source_connection_id=SOURCE_B) is not None


# ----- bound-table FK + CASCADE ----------------------------------------------


class TestBoundTableFK:
    def test_entity_referencing_unknown_table_rejected(self, tmp_path: Path) -> None:
        """An entity must bind to a table already in the store."""
        with (
            SQLiteStore(tmp_path / "store.db") as store,
            pytest.raises(sqlite3.IntegrityError),
        ):
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)

    def test_cascade_delete_sweeps_entity_rows(self, tmp_path: Path) -> None:
        """delete_table on a bound table must remove dependent entities."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            assert store.get_entity("customer", source_connection_id=SOURCE_A) is not None
            store.delete_table("public", "users", source_connection_id=SOURCE_A)
            assert store.get_entity("customer", source_connection_id=SOURCE_A) is None


# ----- Dbt-owned write guard -------------------------------------------------


class TestDbtOwnedWriteGuard:
    def test_dbt_import_overwrites_manual_allowed(self, tmp_path: Path) -> None:
        """Promoting a manual entity to dbt-owned is allowed.

        dbt-import is treated as the source of truth; if a manual
        entity exists with the same name, the import takes over.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(origin="manual"), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(origin="dbt_import"), source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == "dbt_import"

    def test_dbt_import_overwriting_dbt_import_allowed(self, tmp_path: Path) -> None:
        """Re-running `schemabrain import dbt` must be idempotent."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(origin="dbt_import"), source_connection_id=SOURCE_A)
            store.write_entity(
                _customer_entity(origin="dbt_import", description="updated"),
                source_connection_id=SOURCE_A,
            )
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.description == "updated"

    def test_manual_overwriting_dbt_import_rejected(self, tmp_path: Path) -> None:
        """Manual edits to dbt-owned entities are forbidden."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(origin="dbt_import"), source_connection_id=SOURCE_A)
            with pytest.raises(DbtOwnedEntityError, match="dbt"):
                store.write_entity(_customer_entity(origin="manual"), source_connection_id=SOURCE_A)

    def test_suggested_overwriting_dbt_import_rejected(self, tmp_path: Path) -> None:
        """LLM-suggest must also defer to dbt-owned entities."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_entity(_customer_entity(origin="dbt_import"), source_connection_id=SOURCE_A)
            with pytest.raises(DbtOwnedEntityError):
                store.write_entity(
                    _customer_entity(origin="suggested"), source_connection_id=SOURCE_A
                )

    def test_guard_does_not_fire_on_first_write(self, tmp_path: Path) -> None:
        """No existing row → any origin is accepted."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            # Should not raise — write a brand new entity.
            store.write_entity(_customer_entity(origin="manual"), source_connection_id=SOURCE_A)
            got = store.get_entity("customer", source_connection_id=SOURCE_A)
        assert got is not None

    def test_guard_is_per_source(self, tmp_path: Path) -> None:
        """A dbt-owned entity under SOURCE_A doesn't block manual writes under SOURCE_B."""
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(origin="dbt_import"), source_connection_id=SOURCE_A)
            # No raise — different source.
            store.write_entity(_customer_entity(origin="manual"), source_connection_id=SOURCE_B)


# ----- exception type --------------------------------------------------------


class TestExceptionType:
    def test_dbt_owned_entity_error_is_value_error(self) -> None:
        """Callers should be able to catch `DbtOwnedEntityError` as ValueError."""
        assert issubclass(DbtOwnedEntityError, ValueError)
