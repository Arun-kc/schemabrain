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

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        max_rows: int | None = None,
    ) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Mirrors EngineMetricExecutor.max_rows so get_metric's SF-003
        # truncation check (read via getattr) can be exercised with a
        # stub. Defaults None = unbounded, so existing callers are
        # unaffected.
        self.max_rows = max_rows

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
    # Columns beyond `id` exist because tests reference them in
    # group_by / order_by / filter / time_dimension positions. PR-6h.3's
    # compile-time column-existence check raises if a referenced
    # column isn't on the table — fixture must mirror the columns
    # used by tests.
    def _col(name: str, ordinal: int, data_type: str = "text") -> Column:
        return Column(
            name=name,
            table_name="orders",
            schema_name="public",
            data_type=data_type,
            nullable=name != "id",
            ordinal_position=ordinal,
            is_primary_key=name == "id",
        )

    return Table(
        name="orders",
        schema_name="public",
        columns=(
            _col("id", 1, "bigint"),
            _col("user_id", 2, "bigint"),
            _col("status", 3),
            _col("placed_at", 4, "timestamptz"),
            _col("created_at", 5, "timestamptz"),
            _col("total_amount", 6, "integer"),
            _col("billing_address_id", 7, "bigint"),
            _col("shipping_address_id", 8, "bigint"),
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
            # `region` is referenced by group_by tests; PR-6h.3's
            # column-existence check now requires fixture columns to
            # match what tests group_by on, otherwise the validation
            # raises before the test can assert anything.
            Column(
                name="region",
                table_name="customers",
                schema_name="public",
                data_type="text",
                nullable=True,
                ordinal_position=2,
                is_primary_key=False,
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
    # These envelope-mapping / filter tests exercise get_metric mechanics
    # against an UN-tagged seed; they are not about PII enforcement. Opt
    # out of enforcement explicitly with `pii_block=frozenset()` so the
    # fail-closed-on-untagged gate (active whenever pii_block is non-empty)
    # doesn't refuse before execution. build_server now defaults pii_block
    # to the catastrophic-leak floor (SF-005), which would otherwise make
    # this fail closed on a store with no PII tags.
    return build_server(
        store=store,
        source_connection_id=SOURCE,
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        metric_executor=executor,
        pii_block=frozenset(),
    )


def _call(app: Any, args: dict[str, Any]) -> Any:
    """Run `app.call_tool` synchronously, returning the structured
    result. FastMCP's call_tool returns (content, structured)."""
    return asyncio.run(app.call_tool("get_metric", args))


class TestLimitBounds:
    """FZ-GM-007/008: an out-of-range `limit` returns a typed
    `malformed_name` envelope, not a raw FastMCP transport error.

    The Pydantic Field carries no ge/le bound; `get_metric_impl`
    validates in-body and raises `MalformedColumnError`, which the
    server wrapper maps to `malformed_name` — the same graceful shape
    `suggest_joins(max_hops <= 0)` already emits.
    """

    def test_limit_zero_maps_to_malformed_name(self, store_with_seed: SQLiteStore) -> None:
        app = _build(store_with_seed, _StubExecutor(rows=[{"total_revenue": 1}]))
        _content, structured = _call(app, {"name": "total_revenue", "limit": 0})
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "malformed_name"
        assert "limit" in structured["error"]["message"]

    def test_limit_above_cap_maps_to_malformed_name(self, store_with_seed: SQLiteStore) -> None:
        app = _build(store_with_seed, _StubExecutor(rows=[{"total_revenue": 1}]))
        _content, structured = _call(app, {"name": "total_revenue", "limit": 100_000})
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "malformed_name"
        assert "limit" in structured["error"]["message"]

    def test_limit_at_lower_and_upper_bounds_succeed(self, store_with_seed: SQLiteStore) -> None:
        app = _build(store_with_seed, _StubExecutor(rows=[{"total_revenue": 1}]))
        for lim in (1, 10_000):
            _content, structured = _call(app, {"name": "total_revenue", "limit": lim})
            assert structured["status"] == "success", f"limit={lim} is in range, should succeed"


class TestTruncatedFlag:
    """SF-003: `MetricResult.truncated` flags a result whose row count
    hit an applied cap (the `limit` arg or the executor's max_rows), so
    the agent knows the view may be incomplete.
    """

    def test_truncated_true_when_executor_cap_hit(self, store_with_seed: SQLiteStore) -> None:
        rows = [{"total_revenue": i} for i in range(10)]
        app = _build(store_with_seed, _StubExecutor(rows=rows, max_rows=10))
        _content, structured = _call(app, {"name": "total_revenue"})
        assert structured["status"] == "success"
        assert structured["data"]["truncated"] is True

    def test_truncated_false_when_under_every_cap(self, store_with_seed: SQLiteStore) -> None:
        rows = [{"total_revenue": i} for i in range(3)]
        app = _build(store_with_seed, _StubExecutor(rows=rows, max_rows=10))
        _content, structured = _call(app, {"name": "total_revenue"})
        assert structured["status"] == "success"
        assert structured["data"]["truncated"] is False

    def test_truncated_true_when_limit_arg_hit(self, store_with_seed: SQLiteStore) -> None:
        # No executor cap; the returned row count equals the requested
        # `limit`, so there may be more rows beyond it.
        rows = [{"total_revenue": i} for i in range(5)]
        app = _build(store_with_seed, _StubExecutor(rows=rows))
        _content, structured = _call(app, {"name": "total_revenue", "limit": 5})
        assert structured["status"] == "success"
        assert structured["data"]["truncated"] is True


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
        """PR-6h.2 an earlier gap — agent passes `order_by=` with a column
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

    def test_unknown_group_by_column_maps_to_unknown_group_by_column(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """PR-6h.3 stress-test fix — agent passes a `group_by`
        reference where the entity exists but the column doesn't. Pre-
        fix this resolved to literal SQL, ran against Postgres, and
        surfaced as `internal_error` after `UndefinedColumn`. The
        compile-time check raises `UnknownGroupByColumnError` instead,
        mapped here to a clean envelope kind with `describe_entity`
        as the recovery hint + allowed-column list.
        """
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["order.bogus_column"],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unknown_group_by_column"
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "describe_entity"
        suggested = recovery["suggested_args"]
        assert suggested["name"] == "order"
        # The allowed-column list must reflect the actual columns on
        # the orders table; pinning a known column is the cheapest
        # smoke for "the envelope actually carries useful data".
        assert "placed_at" in suggested["allowed_columns"]

    def test_unknown_filter_column_maps_to_unknown_filter_column(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """Parallel to the group_by check — `filters=` with a column
        that doesn't exist also gets the compile-time fix instead of
        leaking `internal_error` from Postgres."""
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "filters": [{"column": "order.bogus_column", "op": "eq", "value": "x"}],
            },
        )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unknown_filter_column"
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "describe_entity"
        assert recovery["suggested_args"]["name"] == "order"

    def test_unknown_measure_column_maps_to_unknown_measure_column(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """A metric whose `measure.column` doesn't exist on the anchor
        table surfaces as `unknown_measure_column` at compile time,
        not as `internal_error` from Postgres. Recovery hints
        `describe_entity` so the operator (or agent) can list the
        actual columns and fix the metric definition.
        """
        from schemabrain.core.metric import Metric, MetricMeasure

        # Overwrite the seeded `total_revenue` with one that references
        # a column not present on `order`.
        store_with_seed.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="bogus_amount"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE,
        )
        executor = _StubExecutor()
        app = _build(store_with_seed, executor)
        _content, structured = _call(app, {"name": "total_revenue"})
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "unknown_measure_column"
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "describe_entity"
        assert recovery["suggested_args"]["name"] == "order"
        assert "bogus_amount" not in recovery["suggested_args"]["allowed_columns"]

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

    def test_ambiguous_time_dimension_maps_to_error(self, tmp_path: Path) -> None:
        """Charter v1.2: when a non-temporal metric has 2+ reachable
        timestamp columns via canonical-join chains, the resolver
        raises `AmbiguousTimeDimensionError` and the envelope maps to
        the `ambiguous_time_dimension` kind. Recovery routes the agent
        back to `get_metric` so it can re-call with explicit guidance.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            # order_item (anchor, no timestamp) -> order (created_at) -> user (signup_at).
            # Both are reachable via many_to_one chains: 2 candidates.
            store.write_table(
                Table(
                    name="order_items",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="order_items",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                        Column(
                            name="order_id",
                            table_name="order_items",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=2,
                        ),
                        Column(
                            name="quantity",
                            table_name="order_items",
                            schema_name="public",
                            data_type="integer",
                            nullable=False,
                            ordinal_position=3,
                        ),
                    ),
                ),
                source_connection_id=SOURCE,
            )
            store.write_table(_orders_table(), source_connection_id=SOURCE)
            store.write_table(
                Table(
                    name="signed_users",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="signed_users",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                        Column(
                            name="signup_at",
                            table_name="signed_users",
                            schema_name="public",
                            data_type="timestamptz",
                            nullable=False,
                            ordinal_position=2,
                        ),
                    ),
                ),
                source_connection_id=SOURCE,
            )
            store.write_entity(
                Entity(
                    name="order_item",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.order_items"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
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
                    name="signed_user",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.signed_users"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
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
                    name="order_signed_user",
                    description="",
                    source_entity="order",
                    target_entity="signed_user",
                    on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality="many_to_one",
                ),
                source_connection_id=SOURCE,
            )
            store.write_metric(
                Metric(
                    name="units_sold",
                    description="",
                    entity="order_item",
                    measure=MetricMeasure(agg="sum", column="quantity"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            executor = _StubExecutor()
            app = _build(store, executor)
            _content, structured = _call(
                app,
                {
                    "name": "units_sold",
                    "time_grain": "month",
                },
            )
        assert structured["status"] == "error"
        assert structured["error"]["kind"] == "ambiguous_time_dimension"
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "get_metric"
        # `recovery.suggested_args` must be populated so a
        # programmatic agent acting on the structured recovery contract
        # can re-call get_metric without parsing the human-readable
        # message. The value names one valid candidate; the agent
        # picks among alternatives based on the user's question.
        assert recovery["suggested_args"] is not None
        assert "time_dimension" in recovery["suggested_args"]
        # Whichever candidate landed first, it must be a real
        # `<entity>.<column>` reference from a reachable timestamp
        # column — orders has both placed_at + created_at, signed_users
        # has signup_at. All three are valid first picks; only the
        # specific BFS ordering decides which one.
        suggested = recovery["suggested_args"]["time_dimension"]
        assert suggested in (
            "order.placed_at",
            "order.created_at",
            "signed_user.signup_at",
        )

    def test_time_dimension_arg_disambiguates_at_mcp_boundary(self, tmp_path: Path) -> None:
        """Positive case: re-calling get_metric with the
        `time_dimension` arg from the prior refusal envelope's
        `recovery.suggested_args` actually succeeds and the inherited
        dimension matches the requested one. Closes the
        ambiguity-error-then-retry loop end-to-end at the MCP seam.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            # Same fixture shape as the ambiguity test above.
            store.write_table(
                Table(
                    name="order_items",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="order_items",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                        Column(
                            name="order_id",
                            table_name="order_items",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=2,
                        ),
                        Column(
                            name="quantity",
                            table_name="order_items",
                            schema_name="public",
                            data_type="integer",
                            nullable=False,
                            ordinal_position=3,
                        ),
                    ),
                ),
                source_connection_id=SOURCE,
            )
            store.write_table(_orders_table(), source_connection_id=SOURCE)
            store.write_table(
                Table(
                    name="signed_users",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="signed_users",
                            schema_name="public",
                            data_type="bigint",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                        Column(
                            name="signup_at",
                            table_name="signed_users",
                            schema_name="public",
                            data_type="timestamptz",
                            nullable=False,
                            ordinal_position=2,
                        ),
                    ),
                ),
                source_connection_id=SOURCE,
            )
            store.write_entity(
                Entity(
                    name="order_item",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.order_items"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
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
                    name="signed_user",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.signed_users"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
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
                    name="order_signed_user",
                    description="",
                    source_entity="order",
                    target_entity="signed_user",
                    on=(JoinColumnPair(source_column="user_id", target_column="id"),),
                    cardinality="many_to_one",
                ),
                source_connection_id=SOURCE,
            )
            store.write_metric(
                Metric(
                    name="units_sold",
                    description="",
                    entity="order_item",
                    measure=MetricMeasure(agg="sum", column="quantity"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            executor = _StubExecutor()
            app = _build(store, executor)
            _content, structured = _call(
                app,
                {
                    "name": "units_sold",
                    "time_grain": "month",
                    "time_dimension": "order.created_at",
                },
            )
        # Disambiguation succeeded — no error, inherited dimension
        # matches the requested one, resolution flips from
        # "unavailable"/ambiguous to "inherited".
        assert structured["status"] == "success"
        data = structured["data"]
        assert data["time_dimension_resolution"] == "inherited"
        assert data["inherited_time_dimension"] == "order.created_at"

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
        # PR-6h.2 an earlier gap: degradation reason surfaced as a closed Literal
        # so agents can switch on the value without parsing text.
        assert structured["degradation_reason"] == "fan_out_join"

    def test_group_by_without_order_by_auto_fills_and_stays_success(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """When the caller asks for `group_by` but no `order_by`,
        the resolver auto-fills ORDER BY with the group columns (ASC)
        so the LIMIT N slice is deterministic. The envelope reports
        `success`, NOT `degraded` — the prior
        `missing_order_by_with_limit` degradation was firing on every
        grouped query without an explicit sort, eroding the meaning of
        `degraded`. Determinism is now built in, no signal needed.
        """
        executor = _StubExecutor(rows=[{"group_col_0": "2024-01-01", "total_revenue": 100}])
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["order.placed_at"],
            },
        )
        assert structured["status"] == "success"
        assert structured["degradation_reason"] is None
        # SQL must contain ORDER BY — proves auto-fill happened, not
        # that the degradation was just silenced. Without ORDER BY the
        # LIMIT slice would be database-default-ordered.
        assert "ORDER BY" in structured["data"]["sql_skeleton"]

    def test_order_by_clears_missing_order_by_degradation(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """When the caller DOES pass `order_by`, the missing-order-by
        degradation is cleared and the envelope surfaces `success` (or
        `degraded` for a different reason like fan_out — but not for
        the order-by gap).
        """
        executor = _StubExecutor(rows=[{"total_revenue": 100}])
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "group_by": ["order.placed_at"],
                "order_by": [{"column": "total_revenue", "direction": "desc"}],
            },
        )
        # store_with_seed doesn't have fan-out, so status should be
        # 'success' with no degradation reason.
        assert structured["status"] == "success"
        assert structured["degradation_reason"] is None

    def test_no_group_by_no_missing_order_by_degradation(
        self, store_with_seed: SQLiteStore
    ) -> None:
        """Single-row aggregate (no group_by) — `LIMIT N` is meaningless,
        no degradation for missing order_by even though limit is set.
        """
        executor = _StubExecutor(rows=[{"total_revenue": 999}])
        app = _build(store_with_seed, executor)
        _content, structured = _call(
            app,
            {
                "name": "total_revenue",
                "limit": 1,
            },
        )
        assert structured["status"] == "success"
        assert structured["degradation_reason"] is None

    def test_time_dimension_unavailable_maps_to_degraded(self, tmp_path: Path) -> None:
        """Charter v1.2: when the metric has no local `time_dimension`
        and the caller passes `time_grain`, the resolver BFSes the
        canonical-join graph for reachable timestamp columns. When
        none are reachable over non-fan-out edges, the plan runs
        unbucketed and the envelope surfaces
        `degradation_reason='time_dimension_unavailable'` so the
        agent can decide whether to widen the metric definition.

        Setup: metric anchored on `customer` (no timestamp), only
        outgoing edge is the reverse-traversal of `customer_orders`
        (originally m:1 from order→customer, walked back it becomes
        1:m → fan-out filter rejects). No reachable timestamp; the
        inheritance step marks the resolution as `unavailable`.
        """
        store = SQLiteStore(tmp_path / "store.db")
        _seed(store)  # gives us order + customer + customer_orders
        # Metric anchored on customer with no time_dimension. The
        # `customer` entity's bound table has no timestamp column;
        # the only join is `order→customer` (m:1), which the
        # inheritance BFS walks backward as a fan-out edge and skips.
        store.write_metric(
            Metric(
                name="customer_count",
                description="",
                entity="customer",
                measure=MetricMeasure(agg="count", column="id"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE,
        )
        executor = _StubExecutor(rows=[{"customer_count": 42}])
        app = _build(store, executor)
        _content, structured = _call(
            app,
            {
                "name": "customer_count",
                "time_grain": "month",
            },
        )
        assert structured["status"] == "degraded"
        assert structured["degradation_reason"] == "time_dimension_unavailable"
        assert structured["data"]["time_dimension_resolution"] == "unavailable"

    def test_pii_blocked_envelope_populates_anchor_in_recovery_args(self, tmp_path: Path) -> None:
        """PII refusal surface: when get_metric refuses on PII policy,
        the envelope's `recovery.suggested_args` carries the metric's
        anchor entity name so an agent can pivot directly to
        `describe_entity(name=<anchor>)` and enumerate non-PII columns.
        Closes the structured-recovery gap on the firewall property #3
        path the README leads with.
        """
        with SQLiteStore(tmp_path / "store.db") as store:
            users = Table(
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
            store.write_table(users, source_connection_id=SOURCE)
            store.write_entity(
                Entity(
                    name="user",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                ),
                source_connection_id=SOURCE,
            )
            store.write_column_pii_tags(
                source_connection_id=SOURCE,
                qualified_table="public.users",
                tags={"email": ("pii", frozenset({"contact"}))},
            )
            store.write_metric(
                Metric(
                    name="email_count",
                    description="",
                    entity="user",
                    measure=MetricMeasure(agg="count", column="email"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE,
            )
            executor = _StubExecutor()
            app = build_server(
                store=store,
                source_connection_id=SOURCE,
                embedder=_FakeEmbedder(),  # type: ignore[arg-type]
                metric_executor=executor,
                pii_block=frozenset({"contact"}),  # type: ignore[arg-type]
            )
            _content, structured = _call(app, {"name": "email_count"})
        assert structured["status"] == "refused"
        assert structured["error"]["kind"] == "pii_blocked"
        recovery = structured["error"]["recovery"]
        assert recovery["suggested_tool"] == "describe_entity"
        # `suggested_args.name` must name the metric's anchor so the
        # agent's follow-up describe_entity call lands on the right
        # entity. Without this, an agent following the structured
        # contract has to fall back to parsing the message string.
        assert recovery["suggested_args"] == {"name": "user"}

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

    def test_max_rows_default_none_returns_all_rows(self) -> None:
        """Back-compat: callers that don't pass max_rows see the full
        result set, exactly as before the flag landed."""
        import sqlalchemy

        from schemabrain.mcp.metric_executor import EngineMetricExecutor

        engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
        try:
            # Generate 5 rows via a recursive CTE (SQLite-portable
            # without needing a real table).
            executor = EngineMetricExecutor(engine)
            rows = executor.execute(
                """
                WITH RECURSIVE seq(n) AS (
                    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 5
                )
                SELECT n FROM seq
                """,
                {},
            )
            assert len(rows) == 5
        finally:
            engine.dispose()

    def test_max_rows_caps_returned_list(self) -> None:
        """`max_rows=3` against a 5-row query returns 3 rows.

        The SQL still runs in full (the cap is application-layer);
        the executor slices after `Result.mappings()` materialises.
        """
        import sqlalchemy

        from schemabrain.mcp.metric_executor import EngineMetricExecutor

        engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
        try:
            executor = EngineMetricExecutor(engine, max_rows=3)
            rows = executor.execute(
                """
                WITH RECURSIVE seq(n) AS (
                    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 5
                )
                SELECT n FROM seq
                """,
                {},
            )
            # Truncated to the first 3 rows.
            assert len(rows) == 3
            assert [r["n"] for r in rows] == [1, 2, 3]
        finally:
            engine.dispose()

    def test_max_rows_no_op_when_result_is_smaller(self) -> None:
        """`max_rows=10` against a 3-row query returns all 3 — no slice
        and no log spam from the truncation branch."""
        import sqlalchemy

        from schemabrain.mcp.metric_executor import EngineMetricExecutor

        engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
        try:
            executor = EngineMetricExecutor(engine, max_rows=10)
            rows = executor.execute(
                """
                WITH RECURSIVE seq(n) AS (
                    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 3
                )
                SELECT n FROM seq
                """,
                {},
            )
            assert len(rows) == 3
        finally:
            engine.dispose()

    def test_max_rows_truncation_logs_warning(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Truncations log a WARNING so operators can see the cap firing
        in their logs / events stream rather than silently shipping
        fewer rows than the SQL produced.

        Uses a captured-call sentinel on the logger rather than caplog
        — the module's logger doesn't propagate to the pytest root
        handler in this test config.
        """
        import sqlalchemy

        from schemabrain.mcp.metric_executor import EngineMetricExecutor

        warnings: list[tuple[str, tuple[object, ...]]] = []

        def _capture(msg: str, *args: object, **_kwargs: object) -> None:
            warnings.append((msg, args))

        monkeypatch.setattr("schemabrain.mcp.metric_executor._logger.warning", _capture)

        engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
        try:
            executor = EngineMetricExecutor(engine, max_rows=2)
            executor.execute(
                """
                WITH RECURSIVE seq(n) AS (
                    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 5
                )
                SELECT n FROM seq
                """,
                {},
            )
            assert any("truncated result" in msg for msg, _ in warnings)
            assert any("--max-rows-per-result" in msg for msg, _ in warnings)
        finally:
            engine.dispose()
        del capsys  # parameter for symmetry; not used
