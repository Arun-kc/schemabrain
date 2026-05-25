"""Tests for `semantic/compiler/emit.py`.

Locks the SQL emission contract:

  - Single statement (no `;`)
  - Parameterised (every literal value enters via `:p_*`)
  - Identifiers from the plan interpolate verbatim (they're store-
    validated as identifier-shape)
  - LIMIT always emitted
  - SELECT columns: time_bucket (when grain), group_cols aliased as
    `group_col_N`, agg(measure) AS metric_name
  - GROUP BY emitted iff time_bucket OR group_by columns present
  - count_distinct → count(DISTINCT col)
  - All six agg literals emit cleanly
  - JOIN per resolved canonical join, with proper ON-pair AND-chained
  - Composite-key joins emit AND-chained ON clauses
  - Predicates: eq/ne/lt/gt/lte/gte/in/not_in/is_null/not_null all emit
    correctly with proper parameter binding
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
    RequestedFilter,
    emit_sql,
    resolve_metric_plan,
)
from schemabrain.semantic.compiler.plan import (
    MetricPlan,
    ResolvedJoin,
)

SOURCE = "src_a"


# ----- fixture helper --------------------------------------------------------


# Columns each fixture table carries beyond `id`. PR-6h.3's compile-
# time column-existence check now validates against the table's column
# list; fixtures must declare every column the tests reference in
# group_by / filter / order_by / time_dimension positions.
_FIXTURE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "orders": (
        ("user_id", "bigint"),
        ("status", "text"),
        ("created_at", "timestamptz"),
        ("shipped_at", "timestamptz"),
        ("refunded_at", "timestamptz"),
        ("total_amount", "integer"),
    ),
    "users": (
        ("region", "text"),
        ("tier", "text"),
    ),
    # `customers` is the table TestJoins.test_composite_join uses
    # under the `customer` entity name (a different binding from
    # `_seed_total_revenue` which points the customer entity at the
    # `users` table). Either table can hold the customer entity, so
    # both column lists need to mirror what tests reference.
    "customers": (
        ("region", "text"),
        ("tier", "text"),
    ),
}


def _simple_table(name: str) -> Table:
    extras = _FIXTURE_COLUMNS.get(name, ())
    columns: tuple[Column, ...] = (
        Column(
            name="id",
            table_name=name,
            schema_name="public",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            is_primary_key=True,
        ),
        *(
            Column(
                name=col_name,
                table_name=name,
                schema_name="public",
                data_type=col_type,
                nullable=True,
                ordinal_position=2 + i,
                is_primary_key=False,
            )
            for i, (col_name, col_type) in enumerate(extras)
        ),
    )
    return Table(name=name, schema_name="public", columns=columns)


def _seed_total_revenue(
    store: SQLiteStore,
    *,
    agg: str = "sum",
    measure_column: str = "total_amount",
    time_dimension: str | None = "order.created_at",
    time_grains: tuple[str, ...] = ("day", "week", "month"),
) -> None:
    store.write_table(_simple_table("orders"), source_connection_id=SOURCE)
    store.write_table(_simple_table("users"), source_connection_id=SOURCE)
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
            measure=MetricMeasure(agg=agg, column=measure_column),  # type: ignore[arg-type]
            time_dimension=time_dimension,
            time_grains=time_grains,  # type: ignore[arg-type]
        ),
        source_connection_id=SOURCE,
    )


# ----- basic shape -----------------------------------------------------------


class TestBasicShape:
    def test_single_statement_no_semicolon(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert ";" not in sql

    def test_minimal_metric_emits_select_from_limit(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, params = emit_sql(plan)
        assert "SELECT" in sql
        # Identifiers are double-quoted in emitted SQL so reserved
        # words (`order`, `user`, etc.) survive Postgres execution.
        assert 'sum("order"."total_amount") AS "total_revenue"' in sql
        assert 'FROM "public"."orders" AS "order"' in sql
        assert "LIMIT :p_limit" in sql
        assert params == {"p_limit": 1000}

    def test_no_group_by_clause_when_no_time_bucket_or_group_by(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert "GROUP BY" not in sql

    def test_no_where_clause_when_no_filters(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert "WHERE" not in sql


# ----- group_by + GROUP BY ---------------------------------------------------


class TestGroupBy:
    def test_group_by_anchor_column_emits_group_clause(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("order.status",),
            )
            sql, params = emit_sql(plan)
        assert '"order"."status" AS group_col_0' in sql
        assert "GROUP BY group_col_0" in sql
        # No params beyond LIMIT for a no-filter query.
        assert set(params.keys()) == {"p_limit"}

    def test_group_by_joined_column_emits_join_clause(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
            )
            sql, _params = emit_sql(plan)
        assert 'JOIN "public"."users" AS "customer" ON "order"."user_id" = "customer"."id"' in sql
        assert '"customer"."region" AS group_col_0' in sql

    def test_group_by_two_columns_emits_two_select_two_group(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("order.status", "customer.region"),
            )
            sql, _params = emit_sql(plan)
        assert '"order"."status" AS group_col_0' in sql
        assert '"customer"."region" AS group_col_1' in sql
        assert "GROUP BY group_col_0, group_col_1" in sql


# ----- time bucket -----------------------------------------------------------


class TestTimeBucket:
    def test_time_grain_emits_date_trunc_literal(self, tmp_path: Path) -> None:
        # TimeGrain is a closed Literal of 5 safe values — the emitter
        # inlines as a quoted literal rather than parameter binding to
        # avoid driver-side text-vs-text ambiguity. See emit.py docs.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                time_grain="day",
            )
            sql, params = emit_sql(plan)
        assert 'date_trunc(\'day\', "order"."created_at") AS time_bucket' in sql
        assert "GROUP BY time_bucket" in sql
        # No `p_bucket` parameter — grain is inline.
        assert "p_bucket" not in params

    def test_time_grain_combined_with_group_by(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.region",),
                time_grain="month",
            )
            sql, params = emit_sql(plan)
        assert "GROUP BY time_bucket, group_col_0" in sql
        assert "date_trunc('month', " in sql
        assert "p_bucket" not in params

    def test_no_time_grain_omits_date_trunc(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert "date_trunc" not in sql

    def test_non_temporal_metric_omits_date_trunc(self, tmp_path: Path) -> None:
        # Even if time_grain were somehow set (resolver would have
        # raised InvalidTimeGrainError, but defensive test), a non-
        # temporal metric never emits date_trunc.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store, time_dimension=None, time_grains=())
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert "date_trunc" not in sql


# ----- aggregations ----------------------------------------------------------


class TestAggregations:
    @pytest.mark.parametrize(
        "agg, expected",
        [
            ("sum", 'sum("order"."total_amount")'),
            ("count", 'count("order"."total_amount")'),
            ("avg", 'avg("order"."total_amount")'),
            ("min", 'min("order"."total_amount")'),
            ("max", 'max("order"."total_amount")'),
        ],
    )
    def test_simple_agg_emits_function_call(self, tmp_path: Path, agg: str, expected: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store, agg=agg)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert expected in sql

    def test_count_distinct_emits_count_distinct(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store, agg="count_distinct", measure_column="user_id")
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
            )
            sql, _params = emit_sql(plan)
        assert 'count(DISTINCT "order"."user_id")' in sql


class TestCompositeExpressionEmit:
    """Composite expressions emit the parsed AST with double-quoting +
    alias-prefix discipline applied to every column operand."""

    def _seed_composite_revenue(
        self, store: SQLiteStore, *, expression: str = "unit_price * quantity"
    ) -> None:
        # `orders` is the anchor table; we extend its fixture column
        # list to include the two operand columns the expression
        # references so the future column-validation pass (commit 4)
        # won't reject this plan.
        nonlocal_columns = _FIXTURE_COLUMNS.setdefault("orders", ())
        if not any(c[0] == "unit_price" for c in nonlocal_columns):
            _FIXTURE_COLUMNS["orders"] = (
                *nonlocal_columns,
                ("unit_price", "integer"),
                ("quantity", "integer"),
            )
        store.write_table(_simple_table("orders"), source_connection_id=SOURCE)
        store.write_entity(
            Entity(
                name="order",
                description="",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id=SOURCE,
        )
        store.write_metric(
            Metric(
                name="line_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", expression=expression),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE,
        )

    def test_composite_multiplication_emits_quoted_operands(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            self._seed_composite_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="line_revenue",
            )
            sql, _params = emit_sql(plan)
        assert 'sum(("order"."unit_price" * "order"."quantity"))' in sql

    def test_composite_with_literal_emits_literal_inline(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            self._seed_composite_revenue(store, expression="unit_price - 100")
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="line_revenue",
            )
            sql, _params = emit_sql(plan)
        assert 'sum(("order"."unit_price" - 100))' in sql

    def test_composite_count_distinct(self, tmp_path: Path) -> None:
        # count_distinct over a composite expression is unusual but
        # structurally valid — the DSL doesn't forbid it. SQL idiom is
        # `count(DISTINCT (a + b))`; emitter should produce exactly that.
        with SQLiteStore(tmp_path / "store.db") as store:
            self._seed_composite_revenue(store)
            # Re-write the metric with count_distinct via the same fixture.
            store.write_metric(
                Metric(
                    name="line_revenue",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="count_distinct", expression="unit_price * quantity"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="line_revenue",
            )
            sql, _params = emit_sql(plan)
        assert 'count(DISTINCT ("order"."unit_price" * "order"."quantity"))' in sql


# ----- filters ---------------------------------------------------------------


class TestFilters:
    def test_eq_filter_emits_parameterised(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.status", op="eq", value="completed"),),
            )
            sql, params = emit_sql(plan)
        assert 'WHERE "order"."status" = :p_filter_0' in sql
        assert params["p_filter_0"] == "completed"
        # No literal value inline.
        assert "completed" not in sql

    @pytest.mark.parametrize(
        "op, expected_op",
        [
            ("ne", "<>"),
            ("lt", "<"),
            ("lte", "<="),
            ("gt", ">"),
            ("gte", ">="),
        ],
    )
    def test_comparison_operators(self, tmp_path: Path, op: str, expected_op: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(
                    RequestedFilter(
                        column="order.total_amount",
                        op=op,
                        value=100,  # type: ignore[arg-type]
                    ),
                ),
            )
            sql, params = emit_sql(plan)
        assert f'"order"."total_amount" {expected_op} :p_filter_0' in sql
        assert params["p_filter_0"] == 100

    def test_in_operator_emits_placeholders(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
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
            sql, params = emit_sql(plan)
        assert '"order"."status" IN (:p_filter_0_0, :p_filter_0_1, :p_filter_0_2)' in sql
        assert params["p_filter_0_0"] == "completed"
        assert params["p_filter_0_1"] == "shipped"
        assert params["p_filter_0_2"] == "refunded"

    def test_not_in_operator(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(
                    RequestedFilter(
                        column="order.status",
                        op="not_in",
                        value=["cancelled"],
                    ),
                ),
            )
            sql, _params = emit_sql(plan)
        assert '"order"."status" NOT IN (:p_filter_0_0)' in sql

    def test_is_null_emits_no_params(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.refunded_at", op="is_null"),),
            )
            sql, params = emit_sql(plan)
        assert '"order"."refunded_at" IS NULL' in sql
        # No filter params (only LIMIT).
        assert set(params.keys()) == {"p_limit"}

    def test_not_null_emits_no_params(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.shipped_at", op="not_null"),),
            )
            sql, _params = emit_sql(plan)
        assert '"order"."shipped_at" IS NOT NULL' in sql

    def test_multiple_filters_and_chained(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(
                    RequestedFilter(column="order.status", op="eq", value="completed"),
                    RequestedFilter(column="order.total_amount", op="gte", value=100),
                ),
            )
            sql, params = emit_sql(plan)
        assert (
            'WHERE "order"."status" = :p_filter_0 AND "order"."total_amount" >= :p_filter_1' in sql
        )
        assert params["p_filter_0"] == "completed"
        assert params["p_filter_1"] == 100


# ----- joins -----------------------------------------------------------------


class TestJoins:
    def test_composite_join_emits_and_chained_on(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_simple_table("orders"), source_connection_id=SOURCE)
            store.write_table(_simple_table("customers"), source_connection_id=SOURCE)
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
                    binding=SingleTableBinding(qualified_table="public.customers"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
            # Composite-key join — multi-tenant case (org_id + user_id).
            store.write_canonical_join(
                CanonicalJoin(
                    name="customer_orders",
                    description="",
                    source_entity="order",
                    target_entity="customer",
                    on=(
                        JoinColumnPair(source_column="org_id", target_column="org_id"),
                        JoinColumnPair(source_column="user_id", target_column="id"),
                    ),
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
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                group_by=("customer.tier",),
            )
            sql, _params = emit_sql(plan)
        assert (
            'JOIN "public"."customers" AS "customer" '
            'ON "order"."org_id" = "customer"."org_id" '
            'AND "order"."user_id" = "customer"."id"' in sql
        )


# ----- LIMIT -----------------------------------------------------------------


class TestLimit:
    def test_custom_limit_emitted(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                limit=42,
            )
            _sql, params = emit_sql(plan)
        assert params["p_limit"] == 42


# ----- parameterisation invariant --------------------------------------------


class TestParameterisationInvariant:
    def test_string_value_with_quotes_never_inline(self, tmp_path: Path) -> None:
        # The most dangerous case — a value containing SQL syntax MUST
        # bind via parameter, never interpolate into the text.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            evil = "completed' OR 1=1 --"
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.status", op="eq", value=evil),),
            )
            sql, params = emit_sql(plan)
        assert evil not in sql
        assert params["p_filter_0"] == evil

    def test_integer_value_binds_as_param(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            plan = resolve_metric_plan(
                store=store,
                source_connection_id=SOURCE,
                metric_name="total_revenue",
                filters=(RequestedFilter(column="order.total_amount", op="gte", value=12345),),
            )
            sql, params = emit_sql(plan)
        # The integer value itself must NOT appear in the SQL text.
        assert "12345" not in sql
        assert params["p_filter_0"] == 12345


# ----- Regression coverage: topological-order invariant defense -------------------


class TestTopologicalOrderDefense:
    """regression test (silent-failure F6): `MetricPlan.joins` is
    contracted to be in topological chain order. If a future refactor
    or a programmatic caller violates that, the emitter would silently
    produce SQL that references an alias before it's introduced — a
    runtime database error several layers away from the bug. The
    emitter now asserts each join's source_alias is already known.
    """

    def test_emit_raises_on_non_topological_plan(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_total_revenue(store)
            # Construct a deliberately non-topological MetricPlan: a
            # ResolvedJoin whose source_alias is "not_yet_introduced"
            # but no prior join introduces that alias.
            anchor_metric = store.get_metric("total_revenue", source_connection_id=SOURCE)
            assert anchor_metric is not None
            bogus_join = ResolvedJoin(
                canonical_name="forward_reference",
                source_alias="not_yet_introduced",
                target_entity="ghost",
                target_table="public.ghost",
                target_alias="ghost",
                on_pairs=(JoinColumnPair(source_column="ghost_id", target_column="id"),),
                cardinality="many_to_one",
            )
            plan = MetricPlan(
                metric=anchor_metric,
                anchor_table="public.orders",
                anchor_alias="order",
                group_by_columns=(),
                time_bucket=None,
                filter_predicates=(),
                limit=1000,
                joins=(bogus_join,),
            )
            with pytest.raises(RuntimeError, match="not topologically ordered"):
                emit_sql(plan)
