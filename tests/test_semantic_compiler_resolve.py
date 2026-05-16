"""Tests for `semantic/compiler/resolve.py`.

Locks the resolver's contract:

  - Unknown metric → UnknownMetricError
  - group_by / filter on anchor entity → ResolvedColumn with anchor
    alias, no join added
  - group_by / filter on entity with 1 canonical join → resolves
    cleanly, JOIN added with correct target alias + on-pairs
  - group_by on entity with 0 canonical joins → UnreachableEntityError
    (if entity exists) or UnknownColumnError (if entity doesn't)
  - group_by on entity with 2+ canonical joins → AmbiguousJoinError
    with all candidate names surfaced
  - time_grain not in metric.time_grains → InvalidTimeGrainError
  - time_grain on non-temporal metric → InvalidTimeGrainError
  - time_grain=None on temporal metric → valid (unbucketed)
  - Malformed column refs → MalformedColumnError
  - Filter operator/value mismatch (unary with value, list-op with
    scalar, scalar-op with list/None) → MalformedColumnError
  - One canonical join + multiple references to its target entity
    deduplicate to one resolved join
  - Resolved joins emit in canonical-name alphabetical order
  - Cardinality flows through to ResolvedJoin so the emit layer can
    surface fan-out warnings
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.semantic.compiler import (
    AmbiguousJoinError,
    InvalidTimeGrainError,
    MalformedColumnError,
    RequestedFilter,
    UnknownColumnError,
    UnknownMetricError,
    UnreachableEntityError,
    resolve_metric_plan,
)

SOURCE = "src_a"


# ----- fixtures --------------------------------------------------------------


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


def _seed_basic(store: SQLiteStore) -> None:
    # `order` (anchored) + `customer` (joinable via 1 canonical) +
    # `total_revenue` (sum over order.total_amount, day/week/month grains)
    store.write_table(_orders_table(), source_connection_id=SOURCE)
    store.write_table(_users_table(), source_connection_id=SOURCE)
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=SOURCE,
    )
    store.write_entity(
        Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.users"),
            identity="id",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="customer_orders",
            description="",
            source_entity="order",
            target_entity="customer",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_amount"),
            time_dimension="order.created_at",
            time_grains=("day", "week", "month"),
        ),
        source_connection_id=SOURCE,
    )


def _seed_multi_canonical(store: SQLiteStore) -> None:
    # `order` ←→ `address` with TWO canonical joins (billing + shipping)
    # — the ambiguity-refusal case.
    store.write_table(_orders_table(), source_connection_id=SOURCE)
    store.write_table(_addresses_table(), source_connection_id=SOURCE)
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=SOURCE,
    )
    store.write_entity(
        Entity(
            name="address",
            description="",
            binding=SingleTableBinding(qualified_table="public.addresses"),
            identity="id",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_billing_address",
            description="",
            source_entity="order",
            target_entity="address",
            on=(JoinColumnPair(source_column="billing_address_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_shipping_address",
            description="",
            source_entity="order",
            target_entity="address",
            on=(JoinColumnPair(source_column="shipping_address_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_amount"),
            time_dimension="order.created_at",
            time_grains=("day", "month"),
        ),
        source_connection_id=SOURCE,
    )


# ----- unknown metric --------------------------------------------------------


class TestUnknownMetric:
    def test_unknown_metric_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(UnknownMetricError, match="ghost_metric"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="ghost_metric",
                )


# ----- happy paths -----------------------------------------------------------


class TestHappyPath:
    def test_metric_only_no_group_by_no_filters(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
        assert plan.metric.name == "total_revenue"
        assert plan.anchor_table == "public.orders"
        assert plan.anchor_alias == "order"
        assert plan.group_by_columns == ()
        assert plan.filter_predicates == ()
        assert plan.time_bucket is None
        assert plan.joins == ()
        assert plan.limit == 1000

    def test_group_by_on_anchor_no_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("order.status",),
            )
        assert plan.joins == ()
        assert len(plan.group_by_columns) == 1
        col = plan.group_by_columns[0]
        assert col.entity == "order"
        assert col.column == "status"
        assert col.qualified_table == "public.orders"
        assert col.alias == "order"

    def test_group_by_on_joined_entity_adds_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
            )
        assert plan.required_join_names == ("customer_orders",)
        assert len(plan.joins) == 1
        join = plan.joins[0]
        assert join.canonical_name == "customer_orders"
        assert join.target_entity == "customer"
        assert join.target_table == "public.users"
        assert join.cardinality == "many_to_one"
        assert join.on_pairs[0].source_column == "user_id"
        assert join.on_pairs[0].target_column == "id"

    def test_two_group_by_columns_on_same_joined_entity_dedupe(self, tmp_path: Path) -> None:
        # Both group_by columns are on `customer` — one JOIN, two
        # group_by columns in the resulting plan.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region", "customer.tier"),
            )
        assert len(plan.joins) == 1
        assert len(plan.group_by_columns) == 2
        assert {c.column for c in plan.group_by_columns} == {"region", "tier"}

    def test_filter_on_anchor(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.status", op="eq", value="completed"),),
            )
        assert plan.joins == ()
        assert len(plan.filter_predicates) == 1
        predicate = plan.filter_predicates[0]
        assert predicate.op == "eq"
        assert predicate.value == "completed"
        assert predicate.param_names == ("p_filter_0",)

    def test_same_entity_in_group_by_and_filter_share_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
                filters=(RequestedFilter(column="customer.tier", op="eq", value="gold"),),
            )
        # ONE join — shared between group_by and filter.
        assert len(plan.joins) == 1

    def test_duplicate_group_by_column_dedupes(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("order.status", "order.status"),
            )
        assert len(plan.group_by_columns) == 1

    def test_time_grain_valid(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                time_grain="day",
            )
        assert plan.time_bucket == "day"

    def test_time_grain_none_against_temporal_metric_ok(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                time_grain=None,
            )
        assert plan.time_bucket is None


# ----- refusals --------------------------------------------------------------


class TestUnreachable:
    def test_existing_entity_no_canonical_join_unreachable(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            # Add an entity (`product`) with NO canonical join from `order`.
            store.write_table(
                Table(
                    name="products",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="products",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                    ),
                ),
                source_connection_id=SOURCE,
            )
            store.write_entity(
                Entity(
                    name="product",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.products"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
            with pytest.raises(UnreachableEntityError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    group_by=("product.name",),
                )
        assert exc_info.value.anchor_entity == "order"
        assert exc_info.value.target_entity == "product"

    def test_nonexistent_entity_unknown_column(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(UnknownColumnError, match="ghost"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    group_by=("ghost.name",),
                )

    def test_ambiguous_join_carries_candidate_names(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_multi_canonical(store)
            with pytest.raises(AmbiguousJoinError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    group_by=("address.country",),
                )
        assert set(exc_info.value.candidate_join_names) == {
            "order_billing_address",
            "order_shipping_address",
        }
        assert exc_info.value.anchor_entity == "order"
        assert exc_info.value.target_entity == "address"


class TestTimeGrainRefusal:
    def test_invalid_time_grain(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(InvalidTimeGrainError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    time_grain="quarter",  # not in (day, week, month)
                )
        assert exc_info.value.requested_grain == "quarter"
        assert exc_info.value.allowed_grains == ("day", "week", "month")

    def test_time_grain_on_non_temporal_metric(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            store.write_metric(
                Metric(
                    name="open_count",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            with pytest.raises(InvalidTimeGrainError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="open_count",
                    time_grain="day",
                )
        assert exc_info.value.allowed_grains == ()


class TestMalformedInputs:
    @pytest.mark.parametrize(
        "bad_ref",
        [
            "no_dot",
            "too.many.dots",
            "1bad.left",
            "right.1bad",
            "",
        ],
    )
    def test_malformed_group_by_column(self, tmp_path: Path, bad_ref: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="group_by"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    group_by=(bad_ref,),
                )

    def test_malformed_filter_column(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="filter"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    filters=(RequestedFilter(column="bare", op="eq", value="x"),),
                )

    def test_unary_op_with_value_rejected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="unary"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    filters=(RequestedFilter(column="order.status", op="is_null", value="x"),),
                )

    def test_list_op_with_scalar_rejected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="list value"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    filters=(RequestedFilter(column="order.status", op="in", value="x"),),
                )

    def test_list_op_empty_list_rejected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="non-empty"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    filters=(RequestedFilter(column="order.status", op="in", value=[]),),
                )

    def test_scalar_op_with_none_rejected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="non-null"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    filters=(RequestedFilter(column="order.status", op="eq", value=None),),
                )

    def test_scalar_op_with_list_rejected(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            with pytest.raises(MalformedColumnError, match="scalar value"):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="total_revenue",
                    filters=(RequestedFilter(column="order.status", op="eq", value=[1, 2]),),
                )


# ----- list-op param names ---------------------------------------------------


class TestListOperatorParams:
    def test_in_operator_generates_one_param_per_value(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(
                    RequestedFilter(
                        column="order.status",
                        op="in",
                        value=["completed", "shipped", "refunded"],
                    ),
                ),
            )
        predicate = plan.filter_predicates[0]
        assert predicate.param_names == (
            "p_filter_0_0",
            "p_filter_0_1",
            "p_filter_0_2",
        )

    def test_unary_op_emits_zero_param_names(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.status", op="is_null", value=None),),
            )
        predicate = plan.filter_predicates[0]
        assert predicate.param_names == ()


# ----- cardinality + fan-out -------------------------------------------------


class TestCardinalityFlow:
    def test_many_to_one_join_not_fan_out(self, tmp_path: Path) -> None:
        # `customer_orders` cardinality is many_to_one — the order rows
        # are NOT fanned out by joining customer. No fan-out warning.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
            )
        assert plan.fan_out_join_names == ()

    def test_one_to_many_join_is_fan_out(self, tmp_path: Path) -> None:
        # Reset the canonical join to one_to_many (anchor "has many"
        # target — e.g. one order has many order_items).
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            store.write_canonical_join(
                CanonicalJoin(
                    name="customer_orders",
                    description="",
                    source_entity="order",
                    target_entity="customer",
                    on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality="one_to_many",
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
            )
        assert plan.fan_out_join_names == ("customer_orders",)

    def test_none_cardinality_treated_as_fan_out(self, tmp_path: Path) -> None:
        # Worst-case treatment: a canonical join without explicit
        # cardinality is treated as many_to_many → warns.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_basic(store)
            store.write_canonical_join(
                CanonicalJoin(
                    name="customer_orders",
                    description="",
                    source_entity="order",
                    target_entity="customer",
                    on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality=None,
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
            )
        assert plan.fan_out_join_names == ("customer_orders",)
