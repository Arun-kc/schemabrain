"""Tests for `semantic/compiler/plan.py` dataclasses + error hierarchy.

Locks the IR's frozen-immutability invariants and the structured-error
fields the MCP tool layer reads when building charter recovery hints.
"""

from __future__ import annotations

import dataclasses

import pytest

from schemabrain.core.join import JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.semantic.compiler import (
    AmbiguousJoinError,
    InvalidTimeGrainError,
    MalformedColumnError,
    MetricCompilerError,
    MetricPlan,
    RequestedFilter,
    ResolvedColumn,
    ResolvedJoin,
    ResolvedPredicate,
    UnknownColumnError,
    UnknownMetricError,
    UnreachableEntityError,
)
from schemabrain.semantic.compiler.plan import (
    GrainMismatchError,
    PiiBlockedError,
)


def _basic_metric() -> Metric:
    return Metric(
        name="total_revenue",
        description="",
        entity="order",
        measure=MetricMeasure(agg="sum", column="total_amount"),
        time_dimension="order.created_at",
        time_grains=("day", "week", "month"),
    )


# ----- ResolvedColumn --------------------------------------------------------


class TestResolvedColumn:
    def test_column_ref_combines_alias_and_column(self) -> None:
        col = ResolvedColumn(
            entity="order",
            column="status",
            qualified_table="public.orders",
            alias="order",
        )
        # Both alias and column are double-quoted so reserved-keyword
        # entity / column names (`order`, `user`, `select`, etc.)
        # survive Postgres execution.
        assert col.column_ref == '"order"."status"'

    def test_is_frozen(self) -> None:
        col = ResolvedColumn(
            entity="order",
            column="status",
            qualified_table="public.orders",
            alias="order",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            col.column = "other"  # type: ignore[misc]


# ----- ResolvedJoin ----------------------------------------------------------


class TestResolvedJoin:
    def test_is_frozen(self) -> None:
        join = ResolvedJoin(
            canonical_name="customer_orders",
            target_entity="customer",
            target_table="public.users",
            target_alias="customer",
            on_pairs=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            join.canonical_name = "other"  # type: ignore[misc]


# ----- RequestedFilter -------------------------------------------------------


class TestRequestedFilter:
    def test_default_value_is_none(self) -> None:
        f = RequestedFilter(column="order.status", op="is_null")
        assert f.value is None

    def test_is_frozen(self) -> None:
        f = RequestedFilter(column="order.status", op="eq", value="completed")
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.value = "other"  # type: ignore[misc]


# ----- MetricPlan ------------------------------------------------------------


class TestMetricPlan:
    def test_required_join_names_pulls_from_joins(self) -> None:
        plan = MetricPlan(
            metric=_basic_metric(),
            anchor_table="public.orders",
            anchor_alias="order",
            group_by_columns=(),
            time_bucket=None,
            filter_predicates=(),
            limit=1000,
            joins=(
                ResolvedJoin(
                    canonical_name="customer_orders",
                    target_entity="customer",
                    target_table="public.users",
                    target_alias="customer",
                    on_pairs=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality="many_to_one",
                ),
                ResolvedJoin(
                    canonical_name="order_product",
                    target_entity="product",
                    target_table="public.products",
                    target_alias="product",
                    on_pairs=(JoinColumnPair(source_column="product_id", target_column="id"),),
                    cardinality="one_to_many",
                ),
            ),
        )
        assert plan.required_join_names == ("customer_orders", "order_product")

    def test_fan_out_join_names_filters_by_cardinality(self) -> None:
        plan = MetricPlan(
            metric=_basic_metric(),
            anchor_table="public.orders",
            anchor_alias="order",
            group_by_columns=(),
            time_bucket=None,
            filter_predicates=(),
            limit=1000,
            joins=(
                ResolvedJoin(
                    canonical_name="customer_orders",
                    target_entity="customer",
                    target_table="public.users",
                    target_alias="customer",
                    on_pairs=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality="many_to_one",  # NOT fan-out
                ),
                ResolvedJoin(
                    canonical_name="order_items",
                    target_entity="order_item",
                    target_table="public.order_items",
                    target_alias="order_item",
                    on_pairs=(JoinColumnPair(source_column="id", target_column="order_id"),),
                    cardinality="one_to_many",  # fan-out
                ),
                ResolvedJoin(
                    canonical_name="ambiguous_cardinality",
                    target_entity="other",
                    target_table="public.other",
                    target_alias="other",
                    on_pairs=(JoinColumnPair(source_column="other_id", target_column="id"),),
                    cardinality=None,  # treated as worst-case
                ),
            ),
        )
        assert plan.fan_out_join_names == ("order_items", "ambiguous_cardinality")

    def test_is_frozen(self) -> None:
        plan = MetricPlan(
            metric=_basic_metric(),
            anchor_table="public.orders",
            anchor_alias="order",
            group_by_columns=(),
            time_bucket=None,
            filter_predicates=(),
            limit=1000,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.limit = 42  # type: ignore[misc]


# ----- error hierarchy -------------------------------------------------------


class TestErrorHierarchy:
    @pytest.mark.parametrize(
        "exc_class",
        [
            UnknownMetricError,
            MalformedColumnError,
            UnknownColumnError,
            GrainMismatchError,
            PiiBlockedError,
        ],
    )
    def test_simple_subclasses_inherit_from_compiler_error(
        self, exc_class: type[Exception]
    ) -> None:
        assert issubclass(exc_class, MetricCompilerError)
        assert issubclass(exc_class, ValueError)

    def test_unreachable_entity_carries_structured_fields(self) -> None:
        exc = UnreachableEntityError(
            anchor_entity="order",
            target_entity="product",
        )
        assert exc.anchor_entity == "order"
        assert exc.target_entity == "product"
        assert "order" in str(exc)
        assert "product" in str(exc)

    def test_ambiguous_join_carries_candidate_names(self) -> None:
        exc = AmbiguousJoinError(
            anchor_entity="order",
            target_entity="address",
            candidate_join_names=("order_billing_address", "order_shipping_address"),
        )
        assert exc.candidate_join_names == (
            "order_billing_address",
            "order_shipping_address",
        )
        assert "billing" in str(exc)

    def test_invalid_time_grain_carries_allowed_grains(self) -> None:
        exc = InvalidTimeGrainError(
            requested_grain="quarter",
            allowed_grains=("day", "week", "month"),
        )
        assert exc.requested_grain == "quarter"
        assert exc.allowed_grains == ("day", "week", "month")
        assert "quarter" in str(exc)

    def test_invalid_time_grain_non_temporal_message(self) -> None:
        # Empty allowed_grains tuple → "metric is non-temporal" message
        # instead of the allowed-list message.
        exc = InvalidTimeGrainError(
            requested_grain="day",
            allowed_grains=(),
        )
        assert "non-temporal" in str(exc)

    def test_unreachable_pickles_with_structured_fields(self) -> None:
        # @dataclass + Exception hybrid would lose the fields on a
        # plain pickle round-trip without __reduce__. The future audit
        # layer may serialise refusal events for log shipping — the
        # round-trip must preserve the structured shape.
        import pickle

        exc = UnreachableEntityError(
            anchor_entity="order",
            target_entity="product",
        )
        restored = pickle.loads(pickle.dumps(exc))
        assert restored.anchor_entity == "order"
        assert restored.target_entity == "product"
        # The args reconstruction also runs __post_init__ so the
        # message is regenerated cleanly.
        assert "product" in str(restored)

    def test_ambiguous_pickles_with_candidate_names(self) -> None:
        import pickle

        exc = AmbiguousJoinError(
            anchor_entity="order",
            target_entity="address",
            candidate_join_names=("order_billing_address", "order_shipping_address"),
        )
        restored = pickle.loads(pickle.dumps(exc))
        assert restored.candidate_join_names == (
            "order_billing_address",
            "order_shipping_address",
        )

    def test_invalid_time_grain_pickles_with_allowed_grains(self) -> None:
        import pickle

        exc = InvalidTimeGrainError(
            requested_grain="quarter",
            allowed_grains=("day", "week", "month"),
        )
        restored = pickle.loads(pickle.dumps(exc))
        assert restored.requested_grain == "quarter"
        assert restored.allowed_grains == ("day", "week", "month")

    def test_resolved_predicate_is_frozen(self) -> None:
        col = ResolvedColumn(
            entity="order",
            column="status",
            qualified_table="public.orders",
            alias="order",
        )
        predicate = ResolvedPredicate(
            column=col,
            op="eq",
            param_names=("p_filter_0",),
            value="completed",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            predicate.value = "other"  # type: ignore[misc]
