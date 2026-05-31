"""Tests for `mcp/resolve_join.py` — the canonical-join lookup tool.

Pins the canonical-join resolve_join truth table:

  - 0 joins → `NoCanonicalJoinError`
  - 1 join, no name → return
  - 1 join, name matches → return
  - 1 join, name doesn't match → `JoinNameMismatchError`
  - 2+ joins, no name → `AmbiguousJoinError`
  - 2+ joins, name matches → return
  - 2+ joins, name doesn't match → `UnknownJoinNameError`

Plus invariants:

  - Direction-insensitive lookup (a, b) == (b, a)
  - Returned `CanonicalJoinInfo` preserves stored direction
  - `sql_skeleton` is paste-ready, composite-key-aware
  - Malformed entity names raise `ValueError`
  - Missing entities raise `EntityNotFoundError` (one error kind for
    either entity — easier surface than two)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.resolve_join import resolve_join_impl
from schemabrain.mcp.shapes import (
    AmbiguousJoinError,
    EntityNotFoundError,
    JoinNameMismatchError,
    NoCanonicalJoinError,
    UnknownJoinNameError,
)

SOURCE_A = "src_a"


# ----- helpers ---------------------------------------------------------------


def _column(name: str, table: str) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type="bigint",
        nullable=False,
        ordinal_position=1,
        is_primary_key=(name == "id"),
    )


def _seed_orders_customer(tmp_path: Path) -> SQLiteStore:
    """Store with users + orders tables + customer + order entities."""
    store = SQLiteStore(tmp_path / "store.db")
    store.write_table(
        Table(name="users", schema_name="public", columns=(_column("id", "users"),)),
        source_connection_id=SOURCE_A,
    )
    store.write_table(
        Table(name="orders", schema_name="public", columns=(_column("id", "orders"),)),
        source_connection_id=SOURCE_A,
    )
    store.write_entity(
        Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        ),
        source_connection_id=SOURCE_A,
    )
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=SOURCE_A,
    )
    return store


def _seed_order_address(tmp_path: Path) -> SQLiteStore:
    """Store with orders + addresses tables + matching entities."""
    store = SQLiteStore(tmp_path / "store.db")
    store.write_table(
        Table(
            name="orders",
            schema_name="public",
            columns=(_column("id", "orders"),),
        ),
        source_connection_id=SOURCE_A,
    )
    store.write_table(
        Table(
            name="addresses",
            schema_name="public",
            columns=(_column("id", "addresses"),),
        ),
        source_connection_id=SOURCE_A,
    )
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=SOURCE_A,
    )
    store.write_entity(
        Entity(
            name="address",
            description="",
            binding=SingleTableBinding(qualified_table="public.addresses"),
            identity="id",
        ),
        source_connection_id=SOURCE_A,
    )
    return store


def _write_customer_orders_join(store: SQLiteStore) -> None:
    store.write_canonical_join(
        CanonicalJoin(
            name="customer_orders",
            description="Links each order to the customer who placed it.",
            source_entity="order",
            target_entity="customer",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
        ),
        source_connection_id=SOURCE_A,
    )


# ----- the design truth table -----------------------------------------------


class TestResolveSingleJoin:
    def test_no_name_returns_only_join(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        store.close()
        assert info.name == "customer_orders"
        assert info.source_entity == "order"
        assert info.target_entity == "customer"

    def test_matching_name_returns_only_join(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
            name="customer_orders",
        )
        store.close()
        assert info.name == "customer_orders"

    def test_mismatched_name_raises_mismatch(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        with pytest.raises(JoinNameMismatchError) as exc_info:
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="order",
                entity_b="customer",
                name="something_else",
            )
        store.close()
        assert exc_info.value.canonical_name == "customer_orders"

    def test_direction_insensitive_lookup(self, tmp_path: Path) -> None:
        # `resolve_join("a", "b")` and `resolve_join("b", "a")` consult
        # the same row set. The returned info preserves the stored
        # direction so the SQL skeleton always renders predictably.
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        forward = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        reverse = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="customer",
            entity_b="order",
        )
        store.close()
        # Same stored direction returned regardless of input order.
        assert forward.source_entity == reverse.source_entity == "order"
        assert forward.target_entity == reverse.target_entity == "customer"
        # Same SQL skeleton — the agent reads identical paste-ready
        # text regardless of how it asked.
        assert forward.sql_skeleton == reverse.sql_skeleton


class TestResolveAmbiguousPair:
    def _seed_billing_and_shipping(self, tmp_path: Path) -> SQLiteStore:
        store = _seed_order_address(tmp_path)
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
        return store

    def test_no_name_raises_ambiguous(self, tmp_path: Path) -> None:
        store = self._seed_billing_and_shipping(tmp_path)
        with pytest.raises(AmbiguousJoinError) as exc_info:
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="order",
                entity_b="address",
            )
        store.close()
        # Both candidate names surface so the agent can disambiguate.
        assert set(exc_info.value.candidate_names) == {
            "order_billing_address",
            "order_shipping_address",
        }

    def test_matching_name_returns_that_one(self, tmp_path: Path) -> None:
        store = self._seed_billing_and_shipping(tmp_path)
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="address",
            name="order_billing_address",
        )
        store.close()
        assert info.name == "order_billing_address"

    def test_non_matching_name_raises_unknown(self, tmp_path: Path) -> None:
        store = self._seed_billing_and_shipping(tmp_path)
        with pytest.raises(UnknownJoinNameError) as exc_info:
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="order",
                entity_b="address",
                name="no_such_join",
            )
        store.close()
        assert set(exc_info.value.candidate_names) == {
            "order_billing_address",
            "order_shipping_address",
        }


class TestResolveNoJoin:
    def test_no_canonical_join_raises(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        # No canonical join written.
        with pytest.raises(NoCanonicalJoinError, match="No canonical join"):
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="order",
                entity_b="customer",
            )
        store.close()


# ----- entity validation -----------------------------------------------------


class TestEntityValidation:
    def test_missing_entity_a_raises_entity_not_found(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        with pytest.raises(EntityNotFoundError, match="ghost"):
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="ghost",
                entity_b="customer",
            )
        store.close()

    def test_missing_entity_b_raises_entity_not_found(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        with pytest.raises(EntityNotFoundError, match="ghost"):
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="order",
                entity_b="ghost",
            )
        store.close()

    @pytest.mark.parametrize("bad", ["", "has space", "with.dot", "1leading_digit"])
    def test_malformed_entity_a_raises_value_error(self, tmp_path: Path, bad: str) -> None:
        store = _seed_orders_customer(tmp_path)
        with pytest.raises(ValueError):
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a=bad,
                entity_b="customer",
            )
        store.close()

    def test_malformed_name_arg_raises_value_error(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        with pytest.raises(ValueError):
            resolve_join_impl(
                store=store,
                source_connection_id=SOURCE_A,
                entity_a="order",
                entity_b="customer",
                name="bad name",
            )
        store.close()


# ----- sql_skeleton rendering -----------------------------------------------


class TestSqlSkeleton:
    def test_single_predicate_renders_paste_ready(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        store.close()
        # Target table name + alias + ON predicate. Identifiers are
        # double-quoted so reserved-keyword aliases (`order`) survive
        # paste-into-Postgres.
        assert info.sql_skeleton == (
            'JOIN "public"."users" AS "customer" ON "order"."user_id" = "customer"."id"'
        )

    def test_composite_predicate_renders_with_and(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        store.write_canonical_join(
            CanonicalJoin(
                name="composite_join",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(
                    JoinColumnPair(source_column="org_id", target_column="org_id"),
                    JoinColumnPair(source_column="user_id", target_column="id"),
                ),
            ),
            source_connection_id=SOURCE_A,
        )
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        store.close()
        assert " AND " in info.sql_skeleton
        assert '"order"."org_id" = "customer"."org_id"' in info.sql_skeleton
        assert '"order"."user_id" = "customer"."id"' in info.sql_skeleton


# ----- PII redaction --------------------------------------------------------


class TestResolveJoinPiiRedaction:
    """The `on[*].source_column` / `target_column` and the
    `sql_skeleton` string must both be scrubbed against
    `pii_block | CATASTROPHIC_LEAK_CATEGORIES` — same posture
    `describe_table` already applies to its FK list. Without this,
    `resolve_join`'s paste-ready SQL is the bypass: it interpolates
    raw column names a determined agent can lift verbatim.
    """

    def test_source_column_catastrophic_tag_masks_pair_and_skeleton(self, tmp_path: Path) -> None:
        # Tag the source column (orders.user_id) as credential. Both
        # the `on[*].source_column` pair AND the sql_skeleton must
        # carry `<redacted_column>`.
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        store.write_column_pii_tags(
            source_connection_id=SOURCE_A,
            qualified_table="public.orders",
            tags={"user_id": ("pii", frozenset({"credential"}))},
        )
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        store.close()
        # Pair list masked on the source side; target side passes through.
        assert info.on[0].source_column == "<redacted_column>"
        assert info.on[0].target_column == "id"
        # The sql_skeleton must NOT contain the raw column name anywhere.
        assert "user_id" not in info.sql_skeleton
        assert "<redacted_column>" in info.sql_skeleton

    def test_target_column_catastrophic_tag_masks_pair_and_skeleton(self, tmp_path: Path) -> None:
        # Tag the target column (users.id) as government_id. Both the
        # `on[*].target_column` pair AND the sql_skeleton mask.
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        store.write_column_pii_tags(
            source_connection_id=SOURCE_A,
            qualified_table="public.users",
            tags={"id": ("pii", frozenset({"government_id"}))},
        )
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        store.close()
        assert info.on[0].source_column == "user_id"
        assert info.on[0].target_column == "<redacted_column>"
        # users.id in the skeleton would be the `"customer"."id"` predicate.
        # After redaction it reads `"customer"."<redacted_column>"`.
        assert '"customer"."id"' not in info.sql_skeleton
        assert '"customer"."<redacted_column>"' in info.sql_skeleton

    def test_composite_join_partial_redaction_preserves_positional_order(
        self, tmp_path: Path
    ) -> None:
        # Composite key (org_id, user_id) → (org_id, id) where users.id
        # is tagged government_id. The redaction must mask the second
        # target column only; positional order in both pair list AND
        # sql_skeleton predicates must be preserved.
        store = _seed_orders_customer(tmp_path)
        store.write_canonical_join(
            CanonicalJoin(
                name="composite_join",
                description="",
                source_entity="order",
                target_entity="customer",
                on=(
                    JoinColumnPair(source_column="org_id", target_column="org_id"),
                    JoinColumnPair(source_column="user_id", target_column="id"),
                ),
            ),
            source_connection_id=SOURCE_A,
        )
        store.write_column_pii_tags(
            source_connection_id=SOURCE_A,
            qualified_table="public.users",
            tags={"id": ("pii", frozenset({"government_id"}))},
        )
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        store.close()
        # Pair order preserved: org_id first, then the redacted slot.
        assert info.on[0].source_column == "org_id"
        assert info.on[0].target_column == "org_id"
        assert info.on[1].source_column == "user_id"
        assert info.on[1].target_column == "<redacted_column>"
        # Composite predicates must render in the same order.
        assert " AND " in info.sql_skeleton
        first_pred_idx = info.sql_skeleton.find('"order"."org_id"')
        second_pred_idx = info.sql_skeleton.find('"order"."user_id"')
        assert 0 < first_pred_idx < second_pred_idx, (
            f"composite predicate order broken in sql_skeleton: {info.sql_skeleton!r}"
        )
        # No raw users.id leak.
        assert '"customer"."id"' not in info.sql_skeleton

    def test_default_pii_block_still_floors_catastrophic(self, tmp_path: Path) -> None:
        # No pii_block kwarg — the catastrophic floor must still apply.
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        store.write_column_pii_tags(
            source_connection_id=SOURCE_A,
            qualified_table="public.orders",
            tags={"user_id": ("pii", frozenset({"payment_card"}))},
        )
        info = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
            # pii_block intentionally omitted.
        )
        store.close()
        assert info.on[0].source_column == "<redacted_column>"
        assert "user_id" not in info.sql_skeleton

    def test_non_floor_category_only_redacted_when_caller_opts_in(self, tmp_path: Path) -> None:
        store = _seed_orders_customer(tmp_path)
        _write_customer_orders_join(store)
        store.write_column_pii_tags(
            source_connection_id=SOURCE_A,
            qualified_table="public.orders",
            tags={"user_id": ("pii", frozenset({"contact"}))},
        )
        # Default: contact is NOT in the floor, so user_id passes through.
        info_default = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
        )
        assert info_default.on[0].source_column == "user_id"
        assert "user_id" in info_default.sql_skeleton

        # Opt-in: caller includes contact in pii_block.
        info_opt_in = resolve_join_impl(
            store=store,
            source_connection_id=SOURCE_A,
            entity_a="order",
            entity_b="customer",
            pii_block=frozenset({"contact"}),
        )
        store.close()
        assert info_opt_in.on[0].source_column == "<redacted_column>"
        assert "user_id" not in info_opt_in.sql_skeleton
