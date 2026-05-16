"""Postgres integration smoke for the metric arc.

Validates that the compiler-emitted SQL — `date_trunc('<grain>', col)`
literal form + double-quoted reserved-keyword identifiers — executes
cleanly against a real Postgres 16 instance.

Requires Docker (testcontainers). Skip with `-m "not integration"`.

The test sequence:
  1. Seed a real Postgres with an `orders` table (matching the
     bundled ecommerce.sql shape).
  2. Insert a few sample rows.
  3. Use a SQLite store seeded with the corresponding entity + metric
     definitions.
  4. Resolve a metric request, emit SQL, execute via
     EngineMetricExecutor backed by the live Postgres.
  5. Assert rows come back with the expected shape.

The point is NOT to verify aggregation arithmetic (the SQL is trivial
sum/count) — it's to verify the compiler's emit format actually
executes against Postgres without surprise, including the reserved-
keyword identifier case (`order` is a Postgres reserved word; the
emitter double-quotes aliases to make it safe).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.metric_executor import EngineMetricExecutor
from schemabrain.semantic.compiler import (
    RequestedFilter,
    emit_sql,
    resolve_metric_plan,
)

_SOURCE = "metrics_smoke"


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
                name="status",
                table_name="orders",
                schema_name="public",
                data_type="text",
                nullable=False,
                ordinal_position=2,
                is_primary_key=False,
            ),
            Column(
                name="total_cents",
                table_name="orders",
                schema_name="public",
                data_type="integer",
                nullable=False,
                ordinal_position=3,
                is_primary_key=False,
            ),
            Column(
                name="placed_at",
                table_name="orders",
                schema_name="public",
                data_type="timestamptz",
                nullable=False,
                ordinal_position=4,
                is_primary_key=False,
            ),
        ),
        foreign_keys=(),
    )


@pytest.fixture(scope="module")
def metric_store(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> SQLiteStore:
    """Set up a Postgres `orders` table with rows + a SQLite store
    seeded with the matching entity + total_revenue metric. Module-
    scoped so the container boot cost is amortised across tests."""
    # Set up Postgres side.
    pg_engine = create_engine(pg_url)
    try:
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS public.orders"))
            conn.execute(
                text(
                    """
                    CREATE TABLE public.orders (
                        id BIGSERIAL PRIMARY KEY,
                        status TEXT NOT NULL,
                        total_cents INTEGER NOT NULL,
                        placed_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO public.orders (status, total_cents, placed_at)
                    VALUES
                        ('completed', 1000, '2024-01-01T10:00:00Z'),
                        ('completed', 2000, '2024-01-01T15:00:00Z'),
                        ('completed', 3000, '2024-01-02T10:00:00Z'),
                        ('cancelled', 500,  '2024-01-02T11:00:00Z')
                    """
                )
            )
    finally:
        pg_engine.dispose()

    # Set up the SQLite store.
    store_path = tmp_path_factory.mktemp("metrics_pg_smoke") / "store.db"
    store = SQLiteStore(store_path)
    store.write_table(_orders_table(), source_connection_id=_SOURCE)
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=_SOURCE,
    )
    store.write_metric(
        Metric(
            name="total_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", column="total_cents"),
            time_dimension="order.placed_at",
            time_grains=("day", "week", "month"),
        ),
        source_connection_id=_SOURCE,
    )
    return store


@pytest.mark.integration
class TestPostgresMetricExecution:
    def test_date_trunc_day_grain_executes(
        self, pg_url: str, metric_store: SQLiteStore
    ) -> None:
        # `date_trunc('day', col)` literal form must execute cleanly
        # against Postgres 16 — locks the emit choice in tests.
        pg_engine = create_engine(pg_url)
        try:
            executor = EngineMetricExecutor(pg_engine)
            plan = resolve_metric_plan(
                store=metric_store,
                source_connection_id=_SOURCE,
                metric_name="total_revenue",
                time_grain="day",
            )
            sql, params = emit_sql(plan)
            # Assert the literal form is what was emitted (identifiers
            # are double-quoted so reserved-keyword names like `order`
            # survive Postgres execution).
            assert 'date_trunc(\'day\', "order"."placed_at")' in sql
            rows = executor.execute(sql, params)
            assert len(rows) >= 1
            # Sum across day buckets must equal the total of completed
            # orders + cancelled in the seed.
            total = sum(int(row["total_revenue"]) for row in rows)
            assert total == 6500  # 1000+2000+3000+500
        finally:
            pg_engine.dispose()

    def test_filter_binding_executes_against_postgres(
        self, pg_url: str, metric_store: SQLiteStore
    ) -> None:
        # Verify a parameterised string filter actually binds via
        # psycopg at runtime, end-to-end.
        pg_engine = create_engine(pg_url)
        try:
            executor = EngineMetricExecutor(pg_engine)
            plan = resolve_metric_plan(
                store=metric_store,
                source_connection_id=_SOURCE,
                metric_name="total_revenue",
                filters=(
                    RequestedFilter(
                        column="order.status", op="eq", value="completed"
                    ),
                ),
            )
            sql, params = emit_sql(plan)
            assert "completed" not in sql  # bound, not inline
            rows = executor.execute(sql, params)
            assert len(rows) == 1
            assert int(rows[0]["total_revenue"]) == 6000  # 1000+2000+3000
        finally:
            pg_engine.dispose()

    def test_in_operator_executes_against_postgres(
        self, pg_url: str, metric_store: SQLiteStore
    ) -> None:
        # Verify the multi-param IN clause binds correctly.
        pg_engine = create_engine(pg_url)
        try:
            executor = EngineMetricExecutor(pg_engine)
            plan = resolve_metric_plan(
                store=metric_store,
                source_connection_id=_SOURCE,
                metric_name="total_revenue",
                filters=(
                    RequestedFilter(
                        column="order.status",
                        op="in",
                        value=["completed", "cancelled"],
                    ),
                ),
            )
            sql, params = emit_sql(plan)
            assert "IN (:p_filter_0_0, :p_filter_0_1)" in sql
            rows = executor.execute(sql, params)
            assert len(rows) == 1
            assert int(rows[0]["total_revenue"]) == 6500
        finally:
            pg_engine.dispose()

    def test_day_week_month_grains_all_execute(
        self, pg_url: str, metric_store: SQLiteStore
    ) -> None:
        # Triple-check that every grain literal Schema Brain emits is
        # actually accepted by Postgres's date_trunc.
        pg_engine = create_engine(pg_url)
        try:
            executor = EngineMetricExecutor(pg_engine)
            for grain in ("day", "week", "month"):
                plan = resolve_metric_plan(
                    store=metric_store,
                    source_connection_id=_SOURCE,
                    metric_name="total_revenue",
                    time_grain=grain,  # type: ignore[arg-type]
                )
                sql, params = emit_sql(plan)
                rows = executor.execute(sql, params)
                assert all("time_bucket" in row for row in rows), (
                    f"grain {grain!r} did not bucket cleanly"
                )
        finally:
            pg_engine.dispose()
