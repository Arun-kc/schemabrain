"""Tests for SQLiteStore canonical-join CRUD + invariants.

Locks the canonical-join store-side contracts:

  - `write_canonical_join` / `get_canonical_join` /
    `list_canonical_joins` / `resolve_canonical_joins` round-trip
  - UPSERT semantics on `(source_connection_id, name)` PK
  - Composite-key `on` lists survive JSON round-trip byte-identical
  - FK to `entities` enforced at SQLite layer — writes referencing
    non-existent entities raise `sqlite3.IntegrityError`
  - Entity-deletion cascade: deleting a referenced entity (via
    `delete_table` cascading through the binding FK) sweeps every
    canonical join that touched it
  - `resolve_canonical_joins` is direction-insensitive AND
    direction-preserving: matches both `(a, b)` and `(b, a)` rows;
    each returned row preserves the stored direction
  - Multi-canonical-per-pair works: two joins between the same
    `(source_entity, target_entity)` pair coexist under distinct names

The entity FK is the canonical enforcement layer — it prevents
canonical joins from outliving their entities and prevents the
suggest pipeline from writing joins against entities the user hasn't
confirmed yet.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

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


def _addresses_table() -> Table:
    return Table(
        name="addresses",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="addresses",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
    )


def _customer_entity() -> Entity:
    return Entity(
        name="customer",
        description="",
        binding=SingleTableBinding(qualified_table="public.users"),
        identity="id",
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
    )


def _address_entity() -> Entity:
    return Entity(
        name="address",
        description="",
        binding=SingleTableBinding(qualified_table="public.addresses"),
        identity="id",
    )


def _customer_orders_join() -> CanonicalJoin:
    return CanonicalJoin(
        name="customer_orders",
        description="Links each order to the customer who placed it.",
        source_entity="order",
        target_entity="customer",
        on=(JoinColumnPair(source_column="user_id", target_column="id"),),
    )


def _seed_customer_order_pair(store: SQLiteStore) -> None:
    store.write_table(_users_table(), source_connection_id=SOURCE_A)
    store.write_table(_orders_table(), source_connection_id=SOURCE_A)
    store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
    store.write_entity(_order_entity(), source_connection_id=SOURCE_A)


# ----- write + read round-trip -----------------------------------------------


class TestWriteReadRoundTrip:
    def test_write_then_get_returns_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            join = _customer_orders_join()
            store.write_canonical_join(join, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got == join

    def test_get_returns_none_for_unknown_name(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            got = store.get_canonical_join("ghost", source_connection_id=SOURCE_A)
        assert got is None

    def test_get_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        # Same canonical join name under two sources stays isolated.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            # SOURCE_B needs its own tables + entities to host a join
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_table(_orders_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_B)
            store.write_entity(_order_entity(), source_connection_id=SOURCE_B)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            got_a = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
            got_b = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_B)
        assert got_a is not None
        assert got_b is None

    def test_join_survives_reopening_the_store(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        join = _customer_orders_join()
        with SQLiteStore(db_path) as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(join, source_connection_id=SOURCE_A)
        with SQLiteStore(db_path) as store:
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got == join

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_all_origin_literals_round_trip(self, tmp_path: Path, origin: str) -> None:
        # Schema symmetry with `entities.origin` — all three values
        # valid at the store layer even though `dbt_import` has no
        # producer at this release.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            join = CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                origin=origin,  # type: ignore[arg-type]
            )
            store.write_canonical_join(join, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == origin


# ----- upsert + timestamps ---------------------------------------------------


class TestUpsertSemantics:
    def test_re_write_updates_description(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            updated = CanonicalJoin(
                name="customer_orders",
                description="A new description.",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            )
            store.write_canonical_join(updated, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.description == "A new description."

    def test_re_write_with_origin_change_succeeds(self, tmp_path: Path) -> None:
        # Joins don't have the dbt-guard symmetry that entities do at
        # v1 — there's no producer for dbt_import joins at this release, so
        # the question of "manual cannot overwrite dbt_import" never
        # arises in production. Verify the store accepts any transition.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            manual = _customer_orders_join()
            store.write_canonical_join(manual, source_connection_id=SOURCE_A)
            suggested = CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                origin="suggested",
            )
            store.write_canonical_join(suggested, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == "suggested"

    def test_re_write_preserves_created_at_advances_updated_at(self, tmp_path: Path) -> None:
        # The asymmetry of `created_at` vs `updated_at` is invariant
        # the canonical-join audit log depends on. Verify by reading
        # the raw row directly.
        db_path = tmp_path / "store.db"
        with SQLiteStore(db_path) as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            # Direct SQL read to inspect timestamps.
            raw_conn = sqlite3.connect(db_path)
            raw_conn.row_factory = sqlite3.Row
            row1 = raw_conn.execute(
                "SELECT created_at, updated_at FROM canonical_joins WHERE name = 'customer_orders'"
            ).fetchone()
            raw_conn.close()
            # Re-write to bump `updated_at`. Bump system clock surrogate by
            # sleeping briefly — int(time.time()) needs to increment.
            import time as _time

            _time.sleep(1.1)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            raw_conn = sqlite3.connect(db_path)
            raw_conn.row_factory = sqlite3.Row
            row2 = raw_conn.execute(
                "SELECT created_at, updated_at FROM canonical_joins WHERE name = 'customer_orders'"
            ).fetchone()
            raw_conn.close()
        assert row1["created_at"] == row2["created_at"]
        assert row2["updated_at"] > row1["updated_at"]


# ----- composite-key round-trip ----------------------------------------------


class TestCompositeKeyRoundTrip:
    def test_composite_on_list_survives_round_trip(self, tmp_path: Path) -> None:
        # Two-column composite-PK join — the JSON serialisation must
        # preserve both pair order AND column order within each pair
        # so the produced SQL skeleton matches the original semantics.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            composite = CanonicalJoin(
                name="composite_join",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(
                    JoinColumnPair(source_column="org_id", target_column="org_id"),
                    JoinColumnPair(source_column="user_id", target_column="id"),
                ),
            )
            store.write_canonical_join(composite, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("composite_join", source_connection_id=SOURCE_A)
        assert got is not None
        assert len(got.on) == 2
        assert got.on[0].source_column == "org_id"
        assert got.on[1].source_column == "user_id"
        assert got == composite


# ----- entity FK enforcement -------------------------------------------------


class TestEntityForeignKey:
    def test_rejects_write_when_source_entity_missing(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_orders_table(), source_connection_id=SOURCE_A)
            # Only the `customer` entity exists — `order` does not.
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_A)
            with pytest.raises(sqlite3.IntegrityError):
                store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)

    def test_rejects_write_when_target_entity_missing(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_orders_table(), source_connection_id=SOURCE_A)
            # Only `order` exists — `customer` does not.
            store.write_entity(_order_entity(), source_connection_id=SOURCE_A)
            with pytest.raises(sqlite3.IntegrityError):
                store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)


# ----- list_canonical_joins --------------------------------------------------


class TestListCanonicalJoins:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.list_canonical_joins(source_connection_id=SOURCE_A) == []

    def test_lists_all_joins_for_source_in_alpha_order(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_table(_addresses_table(), source_connection_id=SOURCE_A)
            store.write_entity(_address_entity(), source_connection_id=SOURCE_A)
            # Write three joins in non-alpha order; expect sorted output.
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            store.write_canonical_join(
                CanonicalJoin(
                    name="order_billing_address",
                    description="",
                    source_entity="order",
                    target_entity="address",
                    on=(
                        JoinColumnPair(
                            source_column="billing_address_id",
                            target_column="id",
                        ),
                    ),
                ),
                source_connection_id=SOURCE_A,
            )
            store.write_canonical_join(
                CanonicalJoin(
                    name="order_shipping_address",
                    description="",
                    source_entity="order",
                    target_entity="address",
                    on=(
                        JoinColumnPair(
                            source_column="shipping_address_id",
                            target_column="id",
                        ),
                    ),
                ),
                source_connection_id=SOURCE_A,
            )
            joins = store.list_canonical_joins(source_connection_id=SOURCE_A)
        names = [j.name for j in joins]
        assert names == [
            "customer_orders",
            "order_billing_address",
            "order_shipping_address",
        ]

    def test_lists_across_sources_when_no_filter(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_table(_orders_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_B)
            store.write_entity(_order_entity(), source_connection_id=SOURCE_B)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_B)
            joins = store.list_canonical_joins()
        # Two rows from the cross-source listing — same name, different
        # sources.
        assert len(joins) == 2


# ----- resolve_canonical_joins -----------------------------------------------


class TestResolveCanonicalJoins:
    def test_resolves_single_join_in_stored_direction(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            # Stored direction: source=order, target=customer.
            forward = store.resolve_canonical_joins(
                "order", "customer", source_connection_id=SOURCE_A
            )
            reverse = store.resolve_canonical_joins(
                "customer", "order", source_connection_id=SOURCE_A
            )
        assert len(forward) == 1
        assert len(reverse) == 1
        # Both lookups return the SAME row with stored direction preserved.
        # The MCP tool layer relies on this for `sql_skeleton` rendering
        # to match the user's confirmed orientation.
        assert forward[0].source_entity == "order"
        assert forward[0].target_entity == "customer"
        assert reverse[0].source_entity == "order"
        assert reverse[0].target_entity == "customer"

    def test_returns_empty_when_no_canonical_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            got = store.resolve_canonical_joins("order", "customer", source_connection_id=SOURCE_A)
        assert got == []

    def test_resolves_multi_canonical_per_pair(self, tmp_path: Path) -> None:
        # The billing/shipping case — two canonical joins between the
        # same `(order, address)` pair, alphabetically ordered.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_table(_addresses_table(), source_connection_id=SOURCE_A)
            store.write_entity(_address_entity(), source_connection_id=SOURCE_A)
            store.write_canonical_join(
                CanonicalJoin(
                    name="order_shipping_address",
                    description="",
                    source_entity="order",
                    target_entity="address",
                    on=(
                        JoinColumnPair(
                            source_column="shipping_address_id",
                            target_column="id",
                        ),
                    ),
                ),
                source_connection_id=SOURCE_A,
            )
            store.write_canonical_join(
                CanonicalJoin(
                    name="order_billing_address",
                    description="",
                    source_entity="order",
                    target_entity="address",
                    on=(
                        JoinColumnPair(
                            source_column="billing_address_id",
                            target_column="id",
                        ),
                    ),
                ),
                source_connection_id=SOURCE_A,
            )
            got = store.resolve_canonical_joins("order", "address", source_connection_id=SOURCE_A)
        assert [j.name for j in got] == [
            "order_billing_address",
            "order_shipping_address",
        ]

    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        # Joins under another source must not surface even when the
        # entity pair matches by name.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            store.write_table(_orders_table(), source_connection_id=SOURCE_B)
            store.write_entity(_customer_entity(), source_connection_id=SOURCE_B)
            store.write_entity(_order_entity(), source_connection_id=SOURCE_B)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            got = store.resolve_canonical_joins("order", "customer", source_connection_id=SOURCE_B)
        assert got == []

    def test_resolves_both_orientations_stored_separately(self, tmp_path: Path) -> None:
        # Two distinct canonical joins between the same entity pair,
        # stored with opposite source/target orientation. The UNION ALL
        # rewrite hits BOTH arms of the index seek and returns both.
        # Documents the symmetry guarantee at the integration layer
        # (resolve_canonical_joins is direction-insensitive AND
        # direction-preserving — each returned row keeps its stored
        # orientation).
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            # First join: order → customer (the natural orientation).
            store.write_canonical_join(
                CanonicalJoin(
                    name="orders_by_customer",
                    description="",
                    source_entity="order",
                    target_entity="customer",
                    on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                ),
                source_connection_id=SOURCE_A,
            )
            # Second join: customer → order (reverse-stored). Unusual
            # in practice but legal at the store layer; pins that the
            # UNION ALL covers it.
            store.write_canonical_join(
                CanonicalJoin(
                    name="customer_to_first_order",
                    description="",
                    source_entity="customer",
                    target_entity="order",
                    on=(JoinColumnPair(source_column="id", target_column="user_id"),),
                ),
                source_connection_id=SOURCE_A,
            )
            got = store.resolve_canonical_joins("order", "customer", source_connection_id=SOURCE_A)
        # Both rows surface; each preserves its stored orientation.
        assert len(got) == 2
        by_name = {j.name: j for j in got}
        assert by_name["orders_by_customer"].source_entity == "order"
        assert by_name["orders_by_customer"].target_entity == "customer"
        assert by_name["customer_to_first_order"].source_entity == "customer"
        assert by_name["customer_to_first_order"].target_entity == "order"


# ----- cascade on entity deletion --------------------------------------------


class TestEntityCascade:
    def test_dropping_source_side_table_sweeps_join_rows(self, tmp_path: Path) -> None:
        # `delete_table` cascades through `entities.binding_table` FK
        # which then cascades through `canonical_joins.source_entity`
        # FK. End state: both the entity and any canonical joins
        # referencing it as SOURCE are gone.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            # Drop the orders table — sweeps `order` entity (the
            # source_entity of customer_orders), which then sweeps
            # the canonical join.
            store.delete_table("public", "orders", source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
            joins = store.list_canonical_joins(source_connection_id=SOURCE_A)
        assert got is None
        assert joins == []

    def test_dropping_target_side_table_sweeps_join_rows(self, tmp_path: Path) -> None:
        # Symmetric coverage — drop the table backing the TARGET entity
        # (not the source). Both FKs carry ON DELETE CASCADE, so both
        # halves of the chain must be tested.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            # Drop the users table — sweeps `customer` entity (the
            # target_entity of customer_orders).
            store.delete_table("public", "users", source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
            joins = store.list_canonical_joins(source_connection_id=SOURCE_A)
        assert got is None
        assert joins == []


# ----- cardinality round-trip -----------------------------------------------


class TestCardinalityRoundTrip:
    def test_none_cardinality_round_trips(self, tmp_path: Path) -> None:
        # Joins authored before the cardinality column existed still
        # round-trip cleanly with `cardinality=None` — the
        # back-compat invariant the dataclass default codifies.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.cardinality is None

    @pytest.mark.parametrize(
        "cardinality",
        ["one_to_one", "one_to_many", "many_to_one", "many_to_many"],
    )
    def test_explicit_cardinality_round_trips(self, tmp_path: Path, cardinality: str) -> None:
        # All four cardinality literals survive write → read. The
        # metric compiler consumes this field to decide fan-out
        # warnings on the get_metric result envelope.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            join = CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                cardinality=cardinality,  # type: ignore[arg-type]
            )
            store.write_canonical_join(join, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.cardinality == cardinality

    def test_cardinality_visible_in_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            join = CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                cardinality="many_to_one",
            )
            store.write_canonical_join(join, source_connection_id=SOURCE_A)
            rows = store.list_canonical_joins(source_connection_id=SOURCE_A)
        assert len(rows) == 1
        assert rows[0].cardinality == "many_to_one"

    def test_cardinality_visible_in_resolve(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            join = CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                cardinality="one_to_many",
            )
            store.write_canonical_join(join, source_connection_id=SOURCE_A)
            resolved = store.resolve_canonical_joins(
                "order", "customer", source_connection_id=SOURCE_A
            )
        assert len(resolved) == 1
        assert resolved[0].cardinality == "one_to_many"

    def test_upsert_overwrites_cardinality(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_customer_order_pair(store)
            store.write_canonical_join(_customer_orders_join(), source_connection_id=SOURCE_A)
            updated = CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                cardinality="many_to_one",
            )
            store.write_canonical_join(updated, source_connection_id=SOURCE_A)
            got = store.get_canonical_join("customer_orders", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.cardinality == "many_to_one"
