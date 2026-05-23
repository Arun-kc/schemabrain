"""Charter v1.2 time-dimension inheritance tests.

Cover the resolve-time BFS over canonical-join chains that picks an
inheritable timestamp column when the metric author didn't declare one
locally:

  - **Local time_dimension untouched** — when the metric DECLARES a
    time_dimension, the resolver doesn't engage inheritance.
  - **Single reachable timestamp → inherited** — the metric's plan
    carries `time_dimension_resolution="inherited"` and the join chain
    is materialised.
  - **Multiple reachable timestamps → AmbiguousTimeDimensionError** —
    the resolver refuses; recovery is for the agent to pass an
    explicit `time_dimension` (a future PR will wire the override).
  - **No reachable timestamp → unavailable resolution** — the plan
    ships unbucketed and the MCP layer surfaces the degradation
    reason `time_dimension_unavailable`.
  - **Fan-out joins skipped** — a `one_to_many` join from anchor to
    target hides the target's timestamp column from inheritance even
    if the column exists (bucketing on a fan-out side would over-count).
  - **Inherited dimension emits the correct alias** — `date_trunc`
    references the joined entity's alias, not the anchor's.
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
    AmbiguousTimeDimensionError,
    emit_sql,
    resolve_metric_plan,
)

SOURCE = "src_test_time_inherit"


def _col(
    table: str,
    name: str,
    ordinal: int,
    data_type: str = "text",
    *,
    pk: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type=data_type,
        nullable=False,
        ordinal_position=ordinal,
        is_primary_key=pk,
    )


def _orders_table() -> Table:
    return Table(
        name="orders",
        schema_name="public",
        columns=(
            _col("orders", "id", 1, "bigint", pk=True),
            _col("orders", "user_id", 2, "bigint"),
            _col("orders", "created_at", 3, "timestamptz"),
        ),
    )


def _order_items_table() -> Table:
    return Table(
        name="order_items",
        schema_name="public",
        columns=(
            _col("order_items", "id", 1, "bigint", pk=True),
            _col("order_items", "order_id", 2, "bigint"),
            _col("order_items", "quantity", 3, "integer"),
        ),
    )


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            _col("users", "id", 1, "bigint", pk=True),
            _col("users", "signup_at", 2, "timestamptz"),
        ),
    )


def _users_table_no_timestamp() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            _col("users", "id", 1, "bigint", pk=True),
            _col("users", "region", 2),
        ),
    )


def _entity(name: str, *, table: str, identity: str = "id") -> Entity:
    return Entity(
        name=name,
        description="",
        binding=SingleTableBinding(qualified_table=f"public.{table}"),
        identity=identity,
        origin="manual",
    )


def _seed_orderitem_anchored(store: SQLiteStore, *, users_table: Table | None = None) -> None:
    """Order-item anchored metric setup. order_item -> order (m:1) and
    optionally order -> user (m:1). The order_items table has no
    timestamp; inheritance must reach `orders.created_at` (and
    optionally `users.signup_at`).
    """
    # Build the default users_table inside the body (not in the
    # signature) so the call doesn't execute at module import time —
    # ruff B008 + a real correctness reason: function-call defaults
    # capture a single instance shared across every call.
    if users_table is None:
        users_table = _users_table()
    store.write_table(_order_items_table(), source_connection_id=SOURCE)
    store.write_table(_orders_table(), source_connection_id=SOURCE)
    store.write_table(users_table, source_connection_id=SOURCE)
    store.write_entity(_entity("order_item", table="order_items"), source_connection_id=SOURCE)
    store.write_entity(_entity("order", table="orders"), source_connection_id=SOURCE)
    store.write_entity(_entity("user", table="users"), source_connection_id=SOURCE)
    store.write_canonical_join(
        CanonicalJoin(
            name="order_item_order",
            description="",
            source_entity="order_item",
            target_entity="order",
            on=(JoinColumnPair(source_column="order_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_canonical_join(
        CanonicalJoin(
            name="order_user",
            description="",
            source_entity="order",
            target_entity="user",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="many_to_one",
        ),
        source_connection_id=SOURCE,
    )
    store.write_metric(
        Metric(
            name="quantity_sold",
            description="",
            entity="order_item",
            measure=MetricMeasure(agg="sum", column="quantity"),
            time_dimension=None,
            time_grains=(),
        ),
        source_connection_id=SOURCE,
    )


class TestSingleInheritance:
    """A non-temporal metric reachable to exactly one timestamp column
    inherits it.
    """

    def test_single_reachable_timestamp_inherited(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            # Users has no timestamp; only orders.created_at is reachable.
            _seed_orderitem_anchored(store, users_table=_users_table_no_timestamp())
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="quantity_sold",
                time_grain="month",
            )
        assert plan.time_dimension_resolution == "inherited"
        assert plan.inherited_time_dimension == "order.created_at"
        assert plan.time_bucket == "month"
        # The chain is one hop: order_item -> order.
        assert plan.time_dimension_inherited_via == ("order_item_order",)
        # The plan materialises the JOIN required to reach `order`.
        join_names = [j.canonical_name for j in plan.joins]
        assert "order_item_order" in join_names

    def test_inherited_dimension_emits_joined_alias(self, tmp_path: Path) -> None:
        """The emitted SQL must date_trunc against the joined entity's
        alias (`order`), not the anchor alias (`order_item`).
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_orderitem_anchored(store, users_table=_users_table_no_timestamp())
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="quantity_sold",
                time_grain="day",
            )
            sql, _params = emit_sql(plan)
        assert 'date_trunc(\'day\', "order"."created_at")' in sql
        assert "date_trunc('day', \"order_item\"" not in sql


class TestAmbiguousInheritance:
    """Two reachable timestamp columns force the agent to disambiguate."""

    def test_two_candidates_raise_ambiguous(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            # Both orders.created_at AND users.signup_at are reachable.
            _seed_orderitem_anchored(store)
            with pytest.raises(AmbiguousTimeDimensionError) as exc:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="quantity_sold",
                    time_grain="month",
                )
        candidate_columns = {c[0] for c in exc.value.candidates}
        assert "order.created_at" in candidate_columns
        assert "user.signup_at" in candidate_columns
        # Recovery hint is rendered as a tuple literal so the agent can
        # paste it back.
        assert "time_dimension=" in str(exc.value)


class TestTimeDimensionDisambiguator:
    """`time_dimension` arg lets the agent pick one of multiple candidates
    after seeing `AmbiguousTimeDimensionError`. Closes the structured-
    recovery loop: the error envelope names the candidates, the agent
    re-calls with the one it wants, the resolver narrows + inherits.
    """

    def test_time_dimension_arg_narrows_to_one_candidate(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_orderitem_anchored(store)
            # Without time_dimension: ambiguous (two reachable timestamps).
            with pytest.raises(AmbiguousTimeDimensionError):
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="quantity_sold",
                    time_grain="month",
                )
            # WITH time_dimension naming a real candidate: succeeds, the
            # named candidate becomes the inherited dimension.
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="quantity_sold",
                time_grain="month",
                time_dimension="order.created_at",
            )
        assert plan.time_dimension_resolution == "inherited"
        assert plan.inherited_time_dimension == "order.created_at"

    def test_time_dimension_arg_not_in_candidates_still_ambiguous(
        self, tmp_path: Path
    ) -> None:
        # An agent that passes a `time_dimension` not in the candidate
        # set falls through to AmbiguousTimeDimensionError — the message
        # carries the full valid list so the next retry can pick a real
        # choice. Treating bogus input as "unavailable" would silently
        # eat the agent's error and ship the wrong shape.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_orderitem_anchored(store)
            with pytest.raises(AmbiguousTimeDimensionError) as exc:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=SOURCE,
                    metric_name="quantity_sold",
                    time_grain="month",
                    time_dimension="bogus.nonexistent_column",
                )
        # The full original candidate set is in the error so the agent's
        # next try has both real options available.
        candidate_columns = {c[0] for c in exc.value.candidates}
        assert "order.created_at" in candidate_columns
        assert "user.signup_at" in candidate_columns


class TestUnavailableInheritance:
    """A non-temporal metric with no reachable timestamp ships unbucketed."""

    def test_no_reachable_timestamp_marks_unavailable(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            # Just order_item alone — no joins, no reachable timestamps.
            store.write_table(_order_items_table(), source_connection_id=SOURCE)
            store.write_entity(
                _entity("order_item", table="order_items"),
                source_connection_id=SOURCE,
            )
            store.write_metric(
                Metric(
                    name="quantity_sold",
                    description="",
                    entity="order_item",
                    measure=MetricMeasure(agg="sum", column="quantity"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="quantity_sold",
                time_grain="day",
            )
        assert plan.time_dimension_resolution == "unavailable"
        assert plan.time_bucket is None
        assert plan.inherited_time_dimension is None


class TestFanOutPathsSkipped:
    """A `one_to_many` join in the chain direction must NOT contribute
    timestamp candidates — bucketing on the fan-out side would
    multiply rows of the anchor.
    """

    def test_one_to_many_target_not_candidate(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            # Reverse-engineered: order is anchor, with one_to_many out
            # to line items (each order has many items). Bucketing on
            # order_items.added_at would inflate aggregates over order.
            store.write_table(_orders_table(), source_connection_id=SOURCE)
            store.write_table(
                Table(
                    name="order_items_dated",
                    schema_name="public",
                    columns=(
                        _col("order_items_dated", "id", 1, "bigint", pk=True),
                        _col("order_items_dated", "order_id", 2, "bigint"),
                        _col(
                            "order_items_dated",
                            "added_at",
                            3,
                            "timestamptz",
                        ),
                    ),
                ),
                source_connection_id=SOURCE,
            )
            store.write_entity(
                _entity("order_no_ts", table="orders"),
                source_connection_id=SOURCE,
            )
            store.write_entity(
                _entity("order_item", table="order_items_dated"),
                source_connection_id=SOURCE,
            )
            store.write_canonical_join(
                CanonicalJoin(
                    name="order_to_items",
                    description="",
                    source_entity="order_no_ts",
                    target_entity="order_item",
                    on=(JoinColumnPair(source_column="id", target_column="order_id"),),
                    cardinality="one_to_many",
                ),
                source_connection_id=SOURCE,
            )
            # Order has its OWN timestamp on `orders.created_at`, but the
            # heuristic excludes the anchor's own table, so the
            # exclusion check should kick in.
            #
            # Override: use a custom orders table WITHOUT created_at so
            # the only timestamp on the graph is on the fan-out side.
            # The seeded `_orders_table` HAS created_at, so use a metric
            # anchored on a third entity that connects via fan-out only.
            store.write_metric(
                Metric(
                    name="order_count",
                    description="",
                    entity="order_no_ts",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="order_count",
                time_grain="day",
            )
        # Orders has created_at on its OWN table — the anchor's own
        # timestamps are excluded by design. The only OTHER candidate
        # would be order_items_dated.added_at, but the fan-out chain
        # `order_no_ts (1) -> order_item (m)` is skipped by the
        # cardinality filter, leaving 0 candidates → unavailable.
        assert plan.time_dimension_resolution == "unavailable"


class TestLocalTimeDimensionUntouched:
    """When the metric DECLARES a time_dimension, inheritance is a no-op."""

    def test_local_time_dim_stays_local(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_orders_table(), source_connection_id=SOURCE)
            store.write_entity(_entity("order", table="orders"), source_connection_id=SOURCE)
            store.write_metric(
                Metric(
                    name="order_volume",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension="order.created_at",
                    time_grains=("day", "week"),
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="order_volume",
                time_grain="day",
            )
        assert plan.time_dimension_resolution == "local"
        assert plan.inherited_time_dimension is None
        assert plan.time_dimension_inherited_via == ()
