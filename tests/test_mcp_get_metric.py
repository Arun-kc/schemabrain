"""Tests for the `get_metric` MCP tool.

Two layers:

  - `get_metric_impl` unit tests with a stub `MetricExecutor` that
    returns canned rows. These verify the resolve→emit→execute→envelope
    pipeline without a real database.

  - Server-level tests via `build_server` + FastMCP's `call_tool`
    that drive the full envelope shape, including each error-kind
    mapping (`unknown_metric`, `unreachable_entity`, `ambiguous_join`,
    `invalid_time_grain`, `malformed_name`, `unknown_name`).

Coverage targets: every error-kind path + the fan-out-degraded path
+ the happy path. Real-DB execution is exercised by the commit-7
Postgres E2E test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp.get_metric import get_metric_impl
from schemabrain.mcp.metric_executor import MetricExecutor
from schemabrain.mcp.server import build_server
from schemabrain.mcp.shapes import MetricFilterArg, MetricResult

SOURCE = "src_a"


# ----- test doubles ----------------------------------------------------------


class _StubExecutor:
    """Canned-row stub. Records the (sql, params) it was called with
    so tests can assert the compiler hooked up properly."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, sql_text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql_text, params))
        return self.rows


class _FakeEmbedder:
    """Minimal Embedder stand-in — `build_server` doesn't call it
    for any tool exercised here, but the seam requires the type."""

    def embed(self, text: str) -> list[float]:  # pragma: no cover — not called
        return [0.0]


# ----- fixture seeding -------------------------------------------------------


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


def _customers_table() -> Table:
    return Table(
        name="customers",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="customers",
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


def _seed(store: SQLiteStore) -> None:
    store.write_table(_orders_table(), source_connection_id=SOURCE)
    store.write_table(_customers_table(), source_connection_id=SOURCE)
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


def _seed_ambiguous(store: SQLiteStore) -> None:
    """Order ←→ address with TWO canonical joins (billing + shipping)."""
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
    for join_name, src_col in (
        ("order_billing_address", "billing_address_id"),
        ("order_shipping_address", "shipping_address_id"),
    ):
        store.write_canonical_join(
            CanonicalJoin(
                name=join_name,
                description="",
                source_entity="order",
                target_entity="address",
                on=(JoinColumnPair(source_column=src_col, target_column="id"),),
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
            time_grains=("day",),
        ),
        source_connection_id=SOURCE,
    )


def _seed_fan_out(store: SQLiteStore) -> None:
    """Order → customer joined with one_to_many cardinality so fan-out
    surfaces."""
    store.write_table(_orders_table(), source_connection_id=SOURCE)
    store.write_table(_customers_table(), source_connection_id=SOURCE)
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
    store.write_canonical_join(
        CanonicalJoin(
            name="customer_orders",
            description="",
            source_entity="order",
            target_entity="customer",
            on=(JoinColumnPair(source_column="user_id", target_column="id"),),
            cardinality="one_to_many",  # Fan-out
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
            time_grains=("day",),
        ),
        source_connection_id=SOURCE,
    )


# ----- impl-level happy path -------------------------------------------------


class TestImplHappyPath:
    def test_no_args_returns_canned_rows(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed(store)
            executor = _StubExecutor(rows=[{"total_revenue": 12345}])
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SOURCE,
                name="total_revenue",
            )
        assert isinstance(result, MetricResult)
        assert result.row_count == 1
        assert result.rows == [{"total_revenue": 12345}]
        assert 'sum("order"."total_amount") AS "total_revenue"' in result.sql_skeleton
        assert "LIMIT :p_limit" in result.sql_skeleton
        # `get_metric_impl` returns the placeholder; the @instrument
        # decorator overwrites it with the real `mcp_audit` row's hex
        # at the server boundary. Tools called impl-directly (this test
        # path) bypass the decorator and see the placeholder.
        assert result.fingerprint == "fp-unset"
        assert result.required_joins == []
        assert result.fan_out_join_names == []
        # Executor was actually called with the compiler's SQL.
        assert len(executor.calls) == 1
        assert executor.calls[0][1]["p_limit"] == 1000

    def test_group_by_invokes_join(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed(store)
            executor = _StubExecutor(rows=[])
            result = get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SOURCE,
                name="total_revenue",
                group_by=("customer.region",),
            )
        assert result.required_joins == ["customer_orders"]
        assert 'JOIN "public"."customers" AS "customer"' in result.sql_skeleton

    def test_filter_values_bind_as_params(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed(store)
            executor = _StubExecutor(rows=[])
            get_metric_impl(
                store=store,
                executor=executor,
                source_connection_id=SOURCE,
                name="total_revenue",
                filters=(MetricFilterArg(column="order.status", op="eq", value="completed"),),
            )
        sql, params = executor.calls[0]
        assert "completed" not in sql
        assert params["p_filter_0"] == "completed"


# ----- impl-level surfacing of compiler errors ------------------------------


class TestImplErrorsRaiseCompilerExceptions:
    def test_unknown_metric_raises(self, tmp_path: Path) -> None:
        from schemabrain.semantic.compiler import UnknownMetricError

        with SQLiteStore(tmp_path / "store.db") as store:
            _seed(store)
            with pytest.raises(UnknownMetricError):
                get_metric_impl(
                    store=store,
                    executor=_StubExecutor(),
                    source_connection_id=SOURCE,
                    name="ghost_metric",
                )


# ----- server-level envelope mapping -----------------------------------------


@pytest.fixture
def store_with_seed(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.db")
    _seed(store)
    return store


@pytest.fixture
def store_with_ambiguous(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.db")
    _seed_ambiguous(store)
    return store


@pytest.fixture
def store_with_fan_out(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "store.db")
    _seed_fan_out(store)
    return store


def _build(store: SQLiteStore, executor: MetricExecutor):
    return build_server(
        store=store,
        source_connection_id=SOURCE,
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        metric_executor=executor,
    )


def _call(app: Any, args: dict[str, Any]) -> Any:
    """Run `app.call_tool` synchronously, returning the structured
    result. FastMCP's call_tool returns (content, structured)."""
    return asyncio.run(app.call_tool("get_metric", args))


class TestEnvelopeMapping:
    def test_happy_path_envelope_status_success(self, store_with_seed: SQLiteStore) -> None:
        executor = _StubExecutor(rows=[{"total_revenue": 12345}])
        app = _build(store_with_seed, executor)
        _content, structured = _call(app, {"name": "total_revenue"})
        assert structured["status"] == "success"
        assert structured["data"]["row_count"] == 1

    def test_unknown_metric_maps_to_unknown_metric_kind(self, store_with_seed: SQLiteStore) -> None:
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(app, {"name": "ghost_metric"})
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unknown_metric"

    def test_malformed_column_maps_to_malformed_name(self, store_with_seed: SQLiteStore) -> None:
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["no_dot_column"],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "malformed_name"

    def test_unknown_column_maps_to_unknown_name(self, store_with_seed: SQLiteStore) -> None:
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["ghost.column"],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unknown_name"

    def test_unreachable_entity_maps_to_unreachable_entity(
        self, store_with_seed: SQLiteStore
    ) -> None:
        # Need an entity that exists but has no canonical join from
        # `order`.
        store_with_seed.write_table(_addresses_table(), source_connection_id=SOURCE)
        store_with_seed.write_entity(
            Entity(
                name="address",
                description="",
                binding=SingleTableBinding(qualified_table="public.addresses"),
                identity="id",
            ),
            source_connection_id=SOURCE,
        )
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["address.country"],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unreachable_entity"
        # Recovery suggests resolve_join with the right entity pair.
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "resolve_join"
        assert recovery["suggested_args"]["entity_a"] == "order"
        assert recovery["suggested_args"]["entity_b"] == "address"

    def test_ambiguous_join_maps_to_ambiguous_join(self, store_with_ambiguous: SQLiteStore) -> None:
        executor = _StubExecutor()
        app = _build(store_with_ambiguous, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["address.country"],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "ambiguous_join"
        recovery = structured["error"]["recovery"]
        # PR-6h.1: ambiguity recovery now suggests retrying `get_metric`
        # with `via=(join_name,)` rather than bouncing out to
        # `resolve_join`. Keeps the agent in-loop on a single tool.
        assert recovery["suggested_tool"] == "get_metric"
        # The first candidate name surfaces in suggested_args["via"].
        via = recovery["suggested_args"]["via"]
        assert len(via) == 1
        assert via[0] in (
            "order_billing_address",
            "order_shipping_address",
        )

    def test_unknown_order_by_column_maps_to_unknown_order_by_column(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """PR-6h.2 Gap #6 — agent passes `order_by=` with a column
        reference that isn't the metric name or a group_by column.
        Compiler raises `UnknownOrderByColumnError`; MCP layer maps to
        `unknown_order_by_column` envelope kind with `allowed_columns`
        in the recovery payload.
        """
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["order.placed_at"],
                "order_by": [{"column": "order.user_id", "direction": "desc"}],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unknown_order_by_column"
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "get_metric"
        # Allowed columns are surfaced to the agent so the retry is
        # mechanical — pick any of these and resubmit.
        allowed = recovery["suggested_args"]["allowed_columns"]
        assert "total_revenue" in allowed
        assert "order.placed_at" in allowed

    def test_invalid_time_grain_maps_to_invalid_time_grain(
        self, store_with_seed: SQLiteStore
    ) -> None:
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "time_grain": "quarter",
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "invalid_time_grain"

    def test_unknown_time_grain_string_maps_to_invalid_time_grain(
        self, store_with_seed: SQLiteStore
    ) -> None:
        # A grain string that isn't a valid TimeGrain value at all
        # (e.g. "hour", "fortnight") raises InvalidTimeGrainError at
        # the API seam — caught by the same envelope mapping.
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "time_grain": "fortnight",
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "invalid_time_grain"

    def test_fan_out_join_maps_to_degraded(self, store_with_fan_out: SQLiteStore) -> None:
        # one_to_many join → SQL still executes but envelope status
        # surfaces `degraded` so the agent knows aggregation may
        # double-count.
        executor = _StubExecutor(rows=[{"total_revenue": 999}])
        app = _build(store_with_fan_out, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["customer.region"],
            },
        )
        assert structured["status"] == "degraded"
        assert structured["data"]["fan_out_join_names"] == ["customer_orders"]

    def test_no_executor_returns_internal_error(self, store_with_seed: SQLiteStore) -> None:
        # Build without an executor — get_metric is registered but
        # every call surfaces as `internal_error` with a
        # server-config message. The operator misconfigured serve.
        app = build_server(
            store=store_with_seed,
            source_connection_id=SOURCE,
            embedder=_FakeEmbedder(),  # type: ignore[arg-type]
            metric_executor=None,
        )
        _content, structured = _call(app, {"name": "total_revenue"})
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "internal_error"

    def test_executor_error_surfaces_internal_error(self, store_with_seed: SQLiteStore) -> None:
        class _BrokenExecutor:
            def execute(self, sql_text: str, params: dict[str, Any]) -> list[dict[str, Any]]:
                raise RuntimeError("DB connection refused")

        app = _build(store_with_seed, _BrokenExecutor())
        _content, structured = _call(app, {"name": "total_revenue"})
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "internal_error"


class TestFilterParameterisation:
    def test_in_filter_value_binds_correctly(self, store_with_seed: SQLiteStore) -> None:
        executor = _StubExecutor(rows=[])
        app = _build(store_with_seed, executor)
        _call(
            app,
            {
                "name": "total_revenue",
                "filters": [
                    {
                        "column": "order.status",
                        "op": "in",
                        "value": ["completed", "shipped"],
                    },
                ],
            },
        )
        sql, params = executor.calls[0]
        assert "IN (:p_filter_0_0, :p_filter_0_1)" in sql
        assert params["p_filter_0_0"] == "completed"
        assert params["p_filter_0_1"] == "shipped"


# ----- EngineMetricExecutor (real SQLAlchemy engine) -------------------------


class TestEngineMetricExecutor:
    def test_executes_simple_select_and_returns_rows_as_dicts(self) -> None:
        # Exercise the SQLAlchemy-backed executor against an in-memory
        # SQLite engine. Postgres-specific syntax (date_trunc) isn't
        # exercised here — that's the commit-7 E2E job. This test
        # locks the result-shape contract: rows come back as plain
        # `dict[str, Any]`, params bind correctly, errors wrap as
        # RuntimeError.
        import sqlalchemy

        from schemabrain.mcp.metric_executor import EngineMetricExecutor

        engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
        try:
            executor = EngineMetricExecutor(engine)
            rows = executor.execute(
                "SELECT :name AS who, :age AS age",
                {"name": "alice", "age": 30},
            )
            assert rows == [{"who": "alice", "age": 30}]
        finally:
            engine.dispose()

    def test_database_error_wraps_as_runtime_error(self) -> None:
        import sqlalchemy

        from schemabrain.mcp.metric_executor import EngineMetricExecutor

        engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
        try:
            executor = EngineMetricExecutor(engine)
            with pytest.raises(RuntimeError, match="metric query failed"):
                # Invalid SQL → SQLAlchemy raises; the executor wraps
                # as RuntimeError so the MCP tool can catch one type.
                executor.execute("SELECT * FROM table_that_does_not_exist", {})
        finally:
            engine.dispose()
