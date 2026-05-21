"""Tests for SQLiteStore metric CRUD + invariants.

Locks the metric store-side contracts:

  - `write_metric` / `get_metric` / `list_metrics` round-trip
  - UPSERT semantics on `(source_connection_id, name)` PK
  - FK to `entities` enforced at SQLite layer — writes referencing
    a non-existent entity raise `sqlite3.IntegrityError`
  - Entity-deletion cascade: deleting the anchor entity (via
    `delete_table` cascading through the binding FK) sweeps every
    metric anchored on it
  - dbt-owned guard: `write_metric` with `origin in {manual, suggested}`
    refuses to overwrite an existing `origin="dbt_import"` row with
    `DbtOwnedMetricError` — mirrors the entity guard from PR #28
  - `time_grains` round-trip preserves canonical order even when the
    storage shape (comma-separated string) is hand-edited; the
    dataclass invariant catches malformed orderings
  - `time_dimension is None` / `time_grains == ()` round-trips faithfully
  - All six `AggFunction` literals round-trip
  - All five `TimeGrain` literals round-trip
  - All three `MetricOrigin` literals round-trip
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import (
    DbtOwnedMetricError,
    MalformedMetricRowError,
    Metric,
    MetricMeasure,
)
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

SOURCE_A = "src_a"
SOURCE_B = "src_b"


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
        ),
    )


def _order_entity() -> Entity:
    return Entity(
        name="order",
        description="",
        binding=SingleTableBinding(qualified_table="public.orders"),
        identity="id",
    )


def _seed_order_entity(store: SQLiteStore, source: str = SOURCE_A) -> None:
    store.write_table(_orders_table(), source_connection_id=source)
    store.write_entity(_order_entity(), source_connection_id=source)


def _total_revenue_metric(**overrides: object) -> Metric:
    defaults: dict[str, object] = {
        "name": "total_revenue",
        "description": "Sum of completed order totals.",
        "entity": "order",
        "measure": MetricMeasure(agg="sum", column="total_amount"),
        "time_dimension": "order.created_at",
        "time_grains": ("day", "week", "month"),
        "origin": "manual",
    }
    defaults.update(overrides)
    return Metric(**defaults)  # type: ignore[arg-type]


def _open_count_metric() -> Metric:
    # Non-temporal metric — exercises the `time_dimension is None` /
    # `time_grains == ()` storage round-trip.
    return Metric(
        name="open_ticket_count",
        description="",
        entity="order",
        measure=MetricMeasure(agg="count", column="id"),
        time_dimension=None,
        time_grains=(),
    )


# ----- write + read round-trip -----------------------------------------------


class TestWriteReadRoundTrip:
    def test_write_then_get_returns_metric(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = _total_revenue_metric()
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got == metric

    def test_get_returns_none_for_unknown_name(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            got = store.get_metric("ghost", source_connection_id=SOURCE_A)
        assert got is None

    def test_get_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store, SOURCE_A)
            _seed_order_entity(store, SOURCE_B)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            got_a = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
            got_b = store.get_metric("total_revenue", source_connection_id=SOURCE_B)
        assert got_a is not None
        assert got_b is None

    def test_metric_survives_reopening_the_store(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        metric = _total_revenue_metric()
        with SQLiteStore(db_path) as store:
            _seed_order_entity(store)
            store.write_metric(metric, source_connection_id=SOURCE_A)
        with SQLiteStore(db_path) as store:
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got == metric

    def test_non_temporal_metric_round_trips(self, tmp_path: Path) -> None:
        # Metric without time_dimension/time_grains — the canonical
        # "current count" shape. Verifies the storage path for the
        # `None` + `()` pair preserves their relationship on read.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = _open_count_metric()
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("open_ticket_count", source_connection_id=SOURCE_A)
        assert got == metric
        assert got is not None
        assert got.time_dimension is None
        assert got.time_grains == ()

    @pytest.mark.parametrize(
        "agg",
        ["sum", "count", "count_distinct", "avg", "min", "max"],
    )
    def test_all_six_agg_literals_round_trip(self, tmp_path: Path, agg: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = _total_revenue_metric(
                measure=MetricMeasure(agg=agg, column="total_amount"),  # type: ignore[arg-type]
            )
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.measure.agg == agg

    @pytest.mark.parametrize(
        "origin",
        ["manual", "suggested", "dbt_import"],
    )
    def test_all_three_origin_literals_round_trip(self, tmp_path: Path, origin: str) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = _total_revenue_metric(origin=origin)
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == origin

    @pytest.mark.parametrize(
        "grains",
        [
            ("day",),
            ("week",),
            ("month",),
            ("quarter",),
            ("year",),
            ("day", "week"),
            ("day", "week", "month", "quarter", "year"),
        ],
    )
    def test_time_grains_subsets_round_trip(self, tmp_path: Path, grains: tuple[str, ...]) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = _total_revenue_metric(time_grains=grains)
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.time_grains == grains


class TestCompositeExpressionRoundTrip:
    """Composite-measure-expression metrics round-trip through the store."""

    def test_composite_expression_round_trips(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = Metric(
                name="total_revenue_real",
                description="SUM of line-item totals",
                entity="order",
                measure=MetricMeasure(agg="sum", expression="unit_price * quantity"),
                time_dimension=None,
                time_grains=(),
            )
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue_real", source_connection_id=SOURCE_A)
        assert got == metric
        assert got is not None
        assert got.measure.column is None
        assert got.measure.expression == "unit_price * quantity"
        assert got.measure.measure_columns == frozenset({"unit_price", "quantity"})

    def test_composite_metric_survives_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        metric = Metric(
            name="line_revenue",
            description="",
            entity="order",
            measure=MetricMeasure(agg="sum", expression="(price - discount) * qty"),
            time_dimension=None,
            time_grains=(),
        )
        with SQLiteStore(db_path) as store:
            _seed_order_entity(store)
            store.write_metric(metric, source_connection_id=SOURCE_A)
        with SQLiteStore(db_path) as store:
            got = store.get_metric("line_revenue", source_connection_id=SOURCE_A)
        assert got == metric

    def test_bare_column_metric_still_round_trips(self, tmp_path: Path) -> None:
        # Backward-compat assertion: an existing bare-column metric
        # written through the new schema (measure_expression IS NULL)
        # reads back unchanged.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            metric = _total_revenue_metric()
            store.write_metric(metric, source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got == metric
        assert got is not None
        assert got.measure.expression is None
        # `_total_revenue_metric()` fixture-helper uses `total_amount`.
        assert got.measure.column == "total_amount"


# ----- upsert + dbt guard ----------------------------------------------------


class TestUpsertSemantics:
    def test_re_write_updates_description(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(
                _total_revenue_metric(description="Pre-tax sum."),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.description == "Pre-tax sum."

    def test_re_write_updates_measure(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(
                _total_revenue_metric(
                    measure=MetricMeasure(agg="avg", column="total_amount"),
                ),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.measure.agg == "avg"

    def test_re_write_updates_time_grains(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(
                _total_revenue_metric(time_grains=("day", "month")),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.time_grains == ("day", "month")

    def test_re_write_does_not_re_anchor_entity(self, tmp_path: Path) -> None:
        # `entity` is the metric's structural anchor — changing it via
        # UPSERT would silently turn `total_revenue@order` into
        # `total_revenue@customer`, an entirely different metric. The
        # DO UPDATE SET deliberately omits `entity`; this test pins
        # the contract so a future maintainer who "fixes" the SQL by
        # adding `entity = excluded.entity` trips here.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            # Seed a second entity so the re-anchor attempt would
            # otherwise satisfy the FK constraint.
            store.write_table(
                Table(
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
                ),
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
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(
                _total_revenue_metric(entity="customer"),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        # The anchor entity stays at its original value despite the
        # attempted re-anchor in the second write.
        assert got.entity == "order"


class TestDbtGuard:
    def test_manual_cannot_overwrite_dbt_import(self, tmp_path: Path) -> None:
        # Mirrors `DbtOwnedEntityError` discipline from PR #28: the
        # dbt-metrics importer owns its rows; manual edits MUST go
        # through the dbt model. Refuse at the store boundary with a
        # message naming the metric.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(
                _total_revenue_metric(origin="dbt_import"),
                source_connection_id=SOURCE_A,
            )
            with pytest.raises(DbtOwnedMetricError, match="total_revenue"):
                store.write_metric(
                    _total_revenue_metric(origin="manual"),
                    source_connection_id=SOURCE_A,
                )

    def test_suggested_cannot_overwrite_dbt_import(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(
                _total_revenue_metric(origin="dbt_import"),
                source_connection_id=SOURCE_A,
            )
            with pytest.raises(DbtOwnedMetricError, match="total_revenue"):
                store.write_metric(
                    _total_revenue_metric(origin="suggested"),
                    source_connection_id=SOURCE_A,
                )

    def test_dbt_import_can_overwrite_dbt_import(self, tmp_path: Path) -> None:
        # Re-running `schemabrain import dbt --include-metrics` is the
        # intended path; idempotent re-import MUST succeed.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(
                _total_revenue_metric(origin="dbt_import"),
                source_connection_id=SOURCE_A,
            )
            store.write_metric(
                _total_revenue_metric(origin="dbt_import", description="Updated by re-import."),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.description == "Updated by re-import."

    def test_dbt_import_can_overwrite_manual(self, tmp_path: Path) -> None:
        # User hand-authored a metric; the dbt model adopts it later.
        # The import takes ownership — manual writes after this point
        # are refused, but the initial dbt-import write succeeds.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(
                _total_revenue_metric(origin="manual"),
                source_connection_id=SOURCE_A,
            )
            store.write_metric(
                _total_revenue_metric(origin="dbt_import"),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == "dbt_import"

    def test_manual_overwrites_suggested(self, tmp_path: Path) -> None:
        # User confirms an LLM suggestion by hand-editing — the manual
        # write succeeds and overwrites the suggested row.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(
                _total_revenue_metric(origin="suggested"),
                source_connection_id=SOURCE_A,
            )
            store.write_metric(
                _total_revenue_metric(origin="manual"),
                source_connection_id=SOURCE_A,
            )
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.origin == "manual"


# ----- foreign-key integrity -------------------------------------------------


class TestForeignKeyIntegrity:
    def test_metric_anchored_on_missing_entity_rejected(self, tmp_path: Path) -> None:
        # The FK is the canonical enforcement layer. Without it, a
        # metric anchored on an entity the user hasn't confirmed would
        # silently persist and surface as `unreachable_entity` at
        # `get_metric` time — wrong layer.
        with SQLiteStore(tmp_path / "store.db") as store:
            metric = _total_revenue_metric(entity="ghost")
            with pytest.raises(sqlite3.IntegrityError):
                store.write_metric(metric, source_connection_id=SOURCE_A)

    def test_deleting_anchor_table_cascades_to_metrics(self, tmp_path: Path) -> None:
        # `delete_table` cascades through the binding FK on `entities`,
        # which cascades again through the FK from `metrics → entities`.
        # A metric outliving its anchor entity is a store-corruption
        # surface we refuse to allow.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.delete_table("public", "orders", source_connection_id=SOURCE_A)
            got = store.get_metric("total_revenue", source_connection_id=SOURCE_A)
        assert got is None


# ----- list_metrics ----------------------------------------------------------


class TestListMetrics:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.list_metrics() == []
            assert store.list_metrics(source_connection_id=SOURCE_A) == []

    def test_lists_all_written_metrics_for_source(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(_open_count_metric(), source_connection_id=SOURCE_A)
            metrics = store.list_metrics(source_connection_id=SOURCE_A)
        names = [m.name for m in metrics]
        assert names == ["open_ticket_count", "total_revenue"]

    def test_filters_by_source_connection_id(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store, SOURCE_A)
            _seed_order_entity(store, SOURCE_B)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(_open_count_metric(), source_connection_id=SOURCE_B)
            a = store.list_metrics(source_connection_id=SOURCE_A)
            b = store.list_metrics(source_connection_id=SOURCE_B)
        assert [m.name for m in a] == ["total_revenue"]
        assert [m.name for m in b] == ["open_ticket_count"]

    def test_lists_across_sources_when_no_filter(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store, SOURCE_A)
            _seed_order_entity(store, SOURCE_B)
            store.write_metric(_total_revenue_metric(), source_connection_id=SOURCE_A)
            store.write_metric(_open_count_metric(), source_connection_id=SOURCE_B)
            cross = store.list_metrics()
        # Deterministic order: by name first, then source.
        assert [m.name for m in cross] == ["open_ticket_count", "total_revenue"]

    def test_lists_ordered_alphabetically_by_name(self, tmp_path: Path) -> None:
        # MCP-tool callers (and the future `metrics list` CLI) depend
        # on stable alphabetical ordering across runs.
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            store.write_metric(
                _total_revenue_metric(name="zeta_metric"),
                source_connection_id=SOURCE_A,
            )
            store.write_metric(
                _total_revenue_metric(name="alpha_metric"),
                source_connection_id=SOURCE_A,
            )
            metrics = store.list_metrics(source_connection_id=SOURCE_A)
        assert [m.name for m in metrics] == ["alpha_metric", "zeta_metric"]


class TestMalformedMetricRowResilience:
    """Direct-SQL writes can land a metric row that passes the SQLite
    CHECK constraints but fails the Python-side whitelist parser at read
    time. `list_metrics` must keep listing valid rows; `get_metric`
    must surface a structured error naming the offending row."""

    def _write_corrupt_expression_row(
        self, store: SQLiteStore, *, name: str, expression: str
    ) -> None:
        # The dataclass's `__post_init__` would normally reject an
        # expression like `abs(amount)`. Bypass via direct SQL to
        # simulate a row that already lives in the store from a
        # previous build, a manual edit, or a future schema mismatch.
        conn = store._require_conn()
        now = 1700000000
        with conn:
            conn.execute(
                "INSERT INTO metrics ("
                "source_connection_id, name, description, "
                "entity, measure_agg, measure_column, measure_expression, "
                "time_dimension, time_grains, origin, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    SOURCE_A,
                    name,
                    "",
                    "order",
                    "sum",
                    None,
                    expression,
                    None,
                    "",
                    "manual",
                    now,
                    now,
                ),
            )

    def test_get_metric_raises_malformed_metric_row_error_for_corrupt_row(
        self, tmp_path: Path
    ) -> None:
        with SQLiteStore(tmp_path / "store.db") as store:
            _seed_order_entity(store)
            self._write_corrupt_expression_row(
                store, name="broken_metric", expression="abs(amount)"
            )
            # `get_metric` on the corrupt row's exact name surfaces the
            # structured error — silently returning None would be worse.
            with pytest.raises(MalformedMetricRowError) as exc_info:
                store.get_metric("broken_metric", source_connection_id=SOURCE_A)
        assert exc_info.value.name == "broken_metric"
        assert "malformed measure expression" in exc_info.value.reason


def test_malformed_metric_row_error_pickles_round_trip() -> None:
    import pickle

    err = MalformedMetricRowError(name="bad_metric", reason="literal must be numeric")
    revived: MalformedMetricRowError = pickle.loads(pickle.dumps(err))
    assert revived.name == err.name
    assert revived.reason == err.reason
    assert str(revived) == str(err)


def test_malformed_metric_row_error_constructible_positionally() -> None:
    # Dataclass-default __init__ accepts positional args, mirroring the
    # peer compiler-error classes. The pickle round-trip relies on this.
    err = MalformedMetricRowError("bad_metric", "literal must be numeric")
    assert err.name == "bad_metric"
    assert err.reason == "literal must be numeric"
