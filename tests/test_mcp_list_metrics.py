"""Tests for the `list_metrics` MCP tool.

Mirrors `test_mcp_list_entities.py`. Two layers:

  - `list_metrics_impl` (pure function, store + source_connection_id
    in, `list[MetricSummary]` out) — empty, single, multi alphabetical,
    source filter, temporal-vs-non-temporal field propagation.
  - The wired tool on the FastMCP server — envelope round-trip,
    empty/success status mapping, follow-up hints to `get_metric`
    (success) and `list_entities` (empty).

Closes the day-one discovery gap surfaced by the 2026-05-21 manual
smoke: the wizard wrote 10 metrics to the store; the agent had no
way to enumerate them. Tool descriptions used to direct the agent
to `schemabrain metrics list` from the CLI — agents have no terminal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mcp import build_server
from schemabrain.mcp.envelope import ToolResponse
from schemabrain.mcp.list_metrics import list_metrics_impl
from schemabrain.mcp.shapes import MetricSummary

# ----- helpers ---------------------------------------------------------------


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
                name="total_cents",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
            ),
            Column(
                name="placed_at",
                table_name="orders",
                schema_name="public",
                data_type="timestamptz",
                nullable=False,
                ordinal_position=3,
            ),
        ),
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="A purchase transaction",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
        origin="manual",
    )


def _temporal_metric(name: str = "total_revenue", *, origin: str = "manual") -> Metric:
    """Build a temporal metric (sum + grain bucketing)."""
    return Metric(
        name=name,
        description=f"Total revenue under {name}",
        entity="order",
        measure=MetricMeasure(agg="sum", column="total_cents"),
        time_dimension="order.placed_at",
        time_grains=("day", "week", "month"),
        origin=origin,  # type: ignore[arg-type]
    )


def _non_temporal_metric(name: str = "order_count", *, origin: str = "manual") -> Metric:
    """Build a non-temporal metric (count, no time_dimension/grains)."""
    return Metric(
        name=name,
        description=f"Count metric {name}",
        entity="order",
        measure=MetricMeasure(agg="count", column="id"),
        time_dimension=None,
        time_grains=(),
        origin=origin,  # type: ignore[arg-type]
    )


class _StubEmbedder:
    """Minimal Embedder stub for server wiring tests."""

    model_name = "test"
    dimension = 4

    def embed(self, text: str) -> tuple[float, ...]:
        del text
        return (1.0, 0.0, 0.0, 0.0)


def _build_test_server(tmp_path: Path) -> tuple[object, SQLiteStore]:
    """Build a server backed by a fresh store seeded with the order
    table + an `order` entity (the metric's anchor). Tests inject
    metrics directly via `store.write_metric`.
    """
    store = SQLiteStore(tmp_path / "s.db")
    store.write_table(_orders_table(), source_connection_id="sid")
    store.write_entity(_order_entity(), source_connection_id="sid")
    server = build_server(
        store=store,
        source_connection_id="sid",
        embedder=_StubEmbedder(),
    )
    return server, store


# ----- impl-level tests ------------------------------------------------------


class TestImplHappyPath:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            result = list_metrics_impl(store=store, source_connection_id="sid")
        assert result == []

    def test_single_temporal_metric_returns_summary(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_metric(_temporal_metric(), source_connection_id="sid")
            result = list_metrics_impl(store=store, source_connection_id="sid")
        assert len(result) == 1
        m = result[0]
        assert isinstance(m, MetricSummary)
        assert m.name == "total_revenue"
        assert m.entity == "order"
        assert m.aggregation == "sum"
        assert m.measure_column == "total_cents"
        assert m.time_dimension == "order.placed_at"
        assert m.time_grains == ("day", "week", "month")
        assert m.origin == "manual"

    def test_non_temporal_metric_has_empty_grains(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_metric(_non_temporal_metric(), source_connection_id="sid")
            result = list_metrics_impl(store=store, source_connection_id="sid")
        m = result[0]
        assert m.time_dimension is None
        assert m.time_grains == ()
        assert m.aggregation == "count"

    def test_bare_column_metric_carries_column_and_null_expression(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_metric(_temporal_metric(), source_connection_id="sid")
            result = list_metrics_impl(store=store, source_connection_id="sid")
        m = result[0]
        assert m.measure_column == "total_cents"
        assert m.measure_expression is None

    def test_composite_metric_carries_expression_and_null_column(self, tmp_path: Path) -> None:
        from schemabrain.core.metric import Metric, MetricMeasure

        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_metric(
                Metric(
                    name="line_revenue",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="sum", expression="unit_price * quantity"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id="sid",
            )
            result = list_metrics_impl(store=store, source_connection_id="sid")
        # Two metrics now; find the composite one.
        composite = next(m for m in result if m.name == "line_revenue")
        assert composite.measure_column is None
        assert composite.measure_expression == "unit_price * quantity"

    def test_multiple_metrics_alphabetical(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            # Insert in non-alphabetical order to verify ordering.
            store.write_metric(_temporal_metric("total_revenue"), source_connection_id="sid")
            store.write_metric(_non_temporal_metric("order_count"), source_connection_id="sid")
            result = list_metrics_impl(store=store, source_connection_id="sid")
        assert [m.name for m in result] == ["order_count", "total_revenue"]

    @pytest.mark.parametrize("origin", ["manual", "suggested", "dbt_import"])
    def test_origin_field_propagates(self, tmp_path: Path, origin: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid")
            store.write_entity(_order_entity(), source_connection_id="sid")
            store.write_metric(_temporal_metric(origin=origin), source_connection_id="sid")
            result = list_metrics_impl(store=store, source_connection_id="sid")
        assert result[0].origin == origin


class TestImplSourceFilter:
    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            store.write_table(_orders_table(), source_connection_id="sid_a")
            store.write_table(_orders_table(), source_connection_id="sid_b")
            store.write_entity(_order_entity(), source_connection_id="sid_a")
            store.write_metric(_temporal_metric(), source_connection_id="sid_a")
            result_a = list_metrics_impl(store=store, source_connection_id="sid_a")
            result_b = list_metrics_impl(store=store, source_connection_id="sid_b")
        assert len(result_a) == 1
        assert result_b == []


# ----- envelope round-trip tests ---------------------------------------------


class TestEnvelopeEmpty:
    def test_empty_store_yields_empty_status(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            _content, structured = asyncio.run(server.call_tool("list_metrics", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "empty"
        assert envelope.error is None
        assert envelope.data == []
        # Empty envelope routes the agent forward — pointing at
        # `list_entities` lets them see what's curated so they can
        # ask the operator to anchor a metric on one.
        assert "list_entities" in (envelope.follow_up_hints or ())


class TestEnvelopeSuccess:
    def test_returns_success_envelope_with_metrics(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_metric(_temporal_metric(), source_connection_id="sid")
            store.write_metric(_non_temporal_metric(), source_connection_id="sid")
            _content, structured = asyncio.run(server.call_tool("list_metrics", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "success"
        assert envelope.error is None
        assert envelope.data is not None
        assert len(envelope.data) == 2
        assert envelope.confidence == "HIGH"
        # Charter Principle 5: composition hint to compute a metric.
        assert "get_metric" in (envelope.follow_up_hints or ())

    def test_envelope_carries_aggregation_and_grains(self, tmp_path: Path) -> None:
        server, store = _build_test_server(tmp_path)
        try:
            store.write_metric(_temporal_metric(), source_connection_id="sid")
            _content, structured = asyncio.run(server.call_tool("list_metrics", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.data is not None
        # Wire shape gives the agent the exact aggregation + temporal
        # grain set it needs to pick the right `time_grain` for
        # `get_metric` without a second round-trip.
        assert envelope.data[0]["aggregation"] == "sum"
        assert envelope.data[0]["measure_column"] == "total_cents"
        assert envelope.data[0]["time_dimension"] == "order.placed_at"
        assert envelope.data[0]["time_grains"] == ["day", "week", "month"]

    def test_envelope_non_temporal_metric_round_trips(self, tmp_path: Path) -> None:
        """Round-2 fold (Reality Checker M1): pin the parallel-emptiness
        invariant (`time_dimension is None` iff `time_grains == ()`)
        at the wire boundary, not just at `Metric` construction. A
        non-temporal metric should round-trip with `time_dimension=None`
        and `time_grains=[]` — JSON does not distinguish tuple from list.
        """
        server, store = _build_test_server(tmp_path)
        try:
            store.write_metric(_non_temporal_metric(), source_connection_id="sid")
            _content, structured = asyncio.run(server.call_tool("list_metrics", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.data is not None
        assert envelope.data[0]["aggregation"] == "count"
        assert envelope.data[0]["measure_column"] == "id"
        assert envelope.data[0]["time_dimension"] is None
        assert envelope.data[0]["time_grains"] == []

    def test_envelope_success_hint_includes_describe_entity(self, tmp_path: Path) -> None:
        """Round-2 fold (silent-failure-hunter F2): the agent needs
        `describe_entity` to interpret an opaque `measure_column`
        before invoking `get_metric` with a related `group_by`.
        Both `get_metric` and `describe_entity` must appear in the
        success-path follow-up hints.
        """
        server, store = _build_test_server(tmp_path)
        try:
            store.write_metric(_temporal_metric(), source_connection_id="sid")
            _content, structured = asyncio.run(server.call_tool("list_metrics", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        hints = envelope.follow_up_hints or ()
        assert "get_metric" in hints
        assert "describe_entity" in hints


class TestEnvelopeStoreFailure:
    """Round-2 fold (Reality Checker): the `_wrap_internal_error`
    branch in `list_metrics` was uncovered post-commit. A store that
    raises should surface as a structurally-valid `error` envelope,
    not propagate the exception as a raw traceback.
    """

    def test_store_raise_returns_error_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server, store = _build_test_server(tmp_path)
        try:

            def _boom(*_a, **_kw):
                raise RuntimeError("store unavailable")

            monkeypatch.setattr(store, "list_metrics", _boom)
            _content, structured = asyncio.run(server.call_tool("list_metrics", {}))
            envelope = ToolResponse.model_validate(structured)
        finally:
            store.close()
        assert envelope.status == "error"
        assert envelope.error is not None
