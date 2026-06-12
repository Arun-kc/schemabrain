"""Tests for `schemabrain.check.engine` — drift detection across entities,
metrics, and canonical joins.

Goals:

  1. Healthy store + healthy live source produces a `CheckReport` with
     zero drifts and `exit_code == 0`. Healthy counts match the totals.
  2. Each drift kind is detected with the correct `Drift` shape:
     `table_missing`, `identity_column_missing`,
     `measure_column_missing`, `time_dimension_column_missing`,
     `join_column_missing`.
  3. Drift cascade is suppressed: when an entity's bound table is
     missing, downstream metric/join drifts on that table are NOT
     double-reported. The entity drift is the canonical issue;
     reporting four `column_missing` rows under it would be noise.
  4. `(schema, table)` lookups are cached so a 50-metric / 50-join run
     against the same 10 tables hits the live source 10 times, not 100.
  5. Empty store (no entities / metrics / joins) produces an
     empty-drift `CheckReport` with `exit_code == 0`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.check.engine import (
    CheckReport,
    Drift,
    check_drift,
)
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

SOURCE_ID = "src_abc123"

# ----- fake DataSource ------------------------------------------------------


class FakeDataSource:
    """Minimal in-memory DataSource for unit testing.

    Tracks `get_table` call count + the `(schema, table)` keys queried
    so caching tests can assert on the hit pattern. `list_tables` is
    not used by the check engine and intentionally raises if called.
    """

    def __init__(self, tables: dict[tuple[str, str], Table]) -> None:
        self._tables = dict(tables)
        self.calls: list[tuple[str, str]] = []

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        raise AssertionError("check engine should not call list_tables")

    def get_table(self, name: str, schema: str) -> Table:
        self.calls.append((schema, name))
        try:
            return self._tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"Table {schema}.{name} not found") from exc

    def close(self) -> None:
        pass


# ----- fixtures -------------------------------------------------------------


# Live-table column shapes mirror the indexed seed snapshots
# (`_seed_customers_table_row` / `_seed_orders_table_row`) so a healthy
# store reports NO shape drift. Keep this convention in sync with the
# seeds: `id` / `*_id` / `*_cents` are `int`, `*_at` is `timestamptz`,
# everything else `text`; PKs and the int/timestamp columns are NOT NULL.
def _seed_data_type(column: str) -> str:
    if column == "id" or column.endswith(("_id", "_cents")):
        return "int"
    if column.endswith("_at"):
        return "timestamptz"
    return "text"


def _seed_nullable(column: str, is_pk: bool) -> bool:
    return not (is_pk or column.endswith(("_id", "_cents", "_at")))


def _table(
    schema: str,
    name: str,
    columns: list[tuple[str, bool]],
    *,
    data_types: dict[str, str] | None = None,
    nullables: dict[str, bool] | None = None,
) -> Table:
    """Build a live `Table` whose column shapes mirror the indexed seeds
    by default. Pass `data_types` / `nullables` to inject a deliberate
    shape mismatch for type/nullability drift tests."""
    data_types = data_types or {}
    nullables = nullables or {}
    cols = tuple(
        Column(
            name=c,
            table_name=name,
            schema_name=schema,
            data_type=data_types.get(c, _seed_data_type(c)),
            nullable=nullables.get(c, _seed_nullable(c, pk)),
            ordinal_position=i + 1,
            is_primary_key=pk,
        )
        for i, (c, pk) in enumerate(columns)
    )
    return Table(name=name, schema_name=schema, columns=cols)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    """An empty SQLiteStore at a tmp path."""
    store = SQLiteStore(path=tmp_path / "store.db")
    try:
        yield store
    finally:
        store.close()


def _seed_customers_table_row(store: SQLiteStore) -> None:
    """Insert a `tables` row so write_entity's FK check passes."""
    store.write_table(
        Table(
            schema_name="public",
            name="customers",
            columns=(
                Column(
                    name="id",
                    table_name="customers",
                    schema_name="public",
                    data_type="int",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="email",
                    table_name="customers",
                    schema_name="public",
                    data_type="text",
                    nullable=True,
                    ordinal_position=2,
                ),
                Column(
                    name="created_at",
                    table_name="customers",
                    schema_name="public",
                    data_type="timestamptz",
                    nullable=False,
                    ordinal_position=3,
                ),
            ),
        ),
        source_connection_id=SOURCE_ID,
    )


def _seed_orders_table_row(store: SQLiteStore) -> None:
    store.write_table(
        Table(
            schema_name="public",
            name="orders",
            columns=(
                Column(
                    name="id",
                    table_name="orders",
                    schema_name="public",
                    data_type="int",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="customer_id",
                    table_name="orders",
                    schema_name="public",
                    data_type="int",
                    nullable=False,
                    ordinal_position=2,
                ),
                Column(
                    name="total_cents",
                    table_name="orders",
                    schema_name="public",
                    data_type="int",
                    nullable=False,
                    ordinal_position=3,
                ),
                Column(
                    name="placed_at",
                    table_name="orders",
                    schema_name="public",
                    data_type="timestamptz",
                    nullable=False,
                    ordinal_position=4,
                ),
            ),
        ),
        source_connection_id=SOURCE_ID,
    )


# ----- happy path -----------------------------------------------------------


class TestHealthyStore:
    def test_empty_store_reports_no_drifts(self, store: SQLiteStore) -> None:
        source = FakeDataSource({})
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.drifts == ()
        assert report.exit_code == 0
        assert report.total_entities == 0
        assert report.total_metrics == 0
        assert report.total_joins == 0
        # No work to do, no live-source calls.
        assert source.calls == []

    def test_entity_with_matching_table_is_healthy(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)
        store.write_entity(
            Entity(
                name="customer",
                description="A registered customer",
                binding=SingleTableBinding(qualified_table="public.customers"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "customers"): _table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.drifts == ()
        assert report.exit_code == 0
        assert report.entities_healthy == 1


# ----- entity drift ---------------------------------------------------------


class TestEntityDrift:
    def test_missing_table_is_table_missing_drift(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)
        store.write_entity(
            Entity(
                name="customer",
                description="",
                binding=SingleTableBinding(qualified_table="public.customers"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        # Live source has NO customers table.
        source = FakeDataSource({})
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert len(report.drifts) == 1
        drift = report.drifts[0]
        assert drift.def_kind == "entity"
        assert drift.def_name == "customer"
        assert drift.drift_kind == "table_missing"
        assert "public.customers" in drift.detail
        assert report.exit_code == 1
        assert report.entities_healthy == 0

    def test_missing_identity_column_is_identity_column_missing_drift(
        self, store: SQLiteStore
    ) -> None:
        _seed_customers_table_row(store)
        store.write_entity(
            Entity(
                name="customer",
                description="",
                binding=SingleTableBinding(qualified_table="public.customers"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
        # Live customers table exists but `id` is gone.
        source = FakeDataSource(
            {
                ("public", "customers"): _table("public", "customers", [("email", False)]),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert len(report.drifts) == 1
        drift = report.drifts[0]
        assert drift.drift_kind == "identity_column_missing"
        assert "public.customers.id" in drift.detail


# ----- metric drift ---------------------------------------------------------


def _seed_customer_entity(store: SQLiteStore) -> None:
    store.write_entity(
        Entity(
            name="customer",
            description="",
            binding=SingleTableBinding(qualified_table="public.customers"),
            identity="id",
        ),
        source_connection_id=SOURCE_ID,
    )


def _seed_order_entity(store: SQLiteStore) -> None:
    store.write_entity(
        Entity(
            name="order",
            description="",
            binding=SingleTableBinding(qualified_table="public.orders"),
            identity="id",
        ),
        source_connection_id=SOURCE_ID,
    )


class TestMetricDrift:
    def test_measure_column_missing_when_column_dropped_from_live(self, store: SQLiteStore) -> None:
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension="order.placed_at",
                time_grains=("day", "month"),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Live orders table exists with id + placed_at, but total_cents
        # is gone.
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("placed_at", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        # Entity is healthy (id is present). Metric drifts.
        assert report.entities_healthy == 1
        drift = next(d for d in report.drifts if d.def_kind == "metric")
        assert drift.drift_kind == "measure_column_missing"
        assert "total_cents" in drift.detail

    def test_composite_expression_missing_operand_column_surfaces_as_drift(
        self, store: SQLiteStore
    ) -> None:
        # Composite-expression measures reference multiple operand
        # columns. If any operand column disappears from the live
        # table, the metric should drift — same shape as the bare-
        # column case, one drift row per metric (not one per missing
        # operand). The first alphabetically-sorted missing column
        # becomes the drift detail so the surface stays deterministic.
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="line_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", expression="unit_price_cents * quantity"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Live table still has `quantity` but `unit_price_cents` is
        # gone. Drift should name `unit_price_cents` (first
        # alphabetically) as the missing column.
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("placed_at", False), ("quantity", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        drift = next(d for d in report.drifts if d.def_kind == "metric")
        assert drift.drift_kind == "measure_column_missing"
        assert "unit_price_cents" in drift.detail
        # The fix hint must point at `measure.expression`, not
        # `measure.column`, for composite metrics.
        assert "measure.expression" in drift.fix_hint

    def test_time_dimension_column_missing(self, store: SQLiteStore) -> None:
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension="order.placed_at",
                time_grains=("day",),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Live orders has total_cents + id, but no placed_at.
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        drifts = [d for d in report.drifts if d.def_kind == "metric"]
        assert len(drifts) == 1
        assert drifts[0].drift_kind == "time_dimension_column_missing"
        assert "placed_at" in drifts[0].detail

    def test_metric_with_time_dimension_present_is_healthy(self, store: SQLiteStore) -> None:
        # The branch where time_dimension is set AND the column exists
        # — exercises the no-drift fall-through to `metrics_healthy +=
        # 1` after the time-dimension check.
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension="order.placed_at",
                time_grains=("day",),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False), ("placed_at", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.drifts == ()
        assert report.metrics_healthy == 1

    def test_metric_drift_is_suppressed_when_entity_table_missing(self, store: SQLiteStore) -> None:
        # When the entity's bound table is missing entirely, the metric
        # drift is suppressed — the entity drift is the canonical
        # issue. Reporting "measure_column_missing" + "time_dimension_
        # column_missing" under a missing table would just be noise.
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension="order.placed_at",
                time_grains=("day",),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Live source has NO orders table at all.
        source = FakeDataSource({})
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        # Exactly one drift: the entity table-missing.
        assert len(report.drifts) == 1
        assert report.drifts[0].def_kind == "entity"
        assert report.drifts[0].drift_kind == "table_missing"


# ----- canonical-join drift -------------------------------------------------


class TestCanonicalJoinDrift:
    def test_healthy_join(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)
        _seed_orders_table_row(store)
        _seed_customer_entity(store)
        _seed_order_entity(store)
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="customer",
                target_entity="order",
                on=(JoinColumnPair(source_column="id", target_column="customer_id"),),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "customers"): _table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("customer_id", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.drifts == ()
        assert report.joins_healthy == 1

    def test_source_column_missing(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)
        _seed_orders_table_row(store)
        _seed_customer_entity(store)
        _seed_order_entity(store)
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="customer",
                target_entity="order",
                on=(JoinColumnPair(source_column="id", target_column="customer_id"),),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Customer table missing `id`.
        source = FakeDataSource(
            {
                ("public", "customers"): _table("public", "customers", [("email", False)]),
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("customer_id", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        # Both the entity identity drift AND the join column drift should
        # NOT both fire — entity drift takes precedence to avoid noise.
        # The entity drift is the canonical issue; the join's source-
        # column drift is downstream.
        entity_drifts = [d for d in report.drifts if d.def_kind == "entity"]
        join_drifts = [d for d in report.drifts if d.def_kind == "canonical_join"]
        assert len(entity_drifts) == 1
        assert entity_drifts[0].drift_kind == "identity_column_missing"
        # Join is suppressed because the source entity is already drifted.
        assert join_drifts == []

    def test_source_column_missing_when_join_uses_non_identity_column(
        self, store: SQLiteStore
    ) -> None:
        # When the join uses a column that is NOT the entity identity,
        # removing that column drifts the join WITHOUT cascading from
        # an entity drift — exercises the source-side column-check
        # branch on a healthy-entity path.
        _seed_customers_table_row(store)
        _seed_orders_table_row(store)
        _seed_customer_entity(store)
        _seed_order_entity(store)
        # Conceptually: customer.email = order.placed_at (synthetic
        # join just for the test — the engine doesn't validate join
        # semantics, only column existence).
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="customer",
                target_entity="order",
                on=(JoinColumnPair(source_column="email", target_column="placed_at"),),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Customer table keeps identity but drops email → join drift
        # without entity drift.
        source = FakeDataSource(
            {
                ("public", "customers"): _table("public", "customers", [("id", True)]),
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("placed_at", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.entities_healthy == 2
        join_drifts = [d for d in report.drifts if d.def_kind == "canonical_join"]
        assert len(join_drifts) == 1
        assert join_drifts[0].drift_kind == "join_column_missing"
        assert "email" in join_drifts[0].detail

    def test_target_column_missing_when_entities_healthy(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)
        _seed_orders_table_row(store)
        _seed_customer_entity(store)
        _seed_order_entity(store)
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="customer",
                target_entity="order",
                on=(JoinColumnPair(source_column="id", target_column="customer_id"),),
            ),
            source_connection_id=SOURCE_ID,
        )
        # Both entity identities (customer.id, order.id) present, but
        # the FK column on orders (`customer_id`) is gone.
        source = FakeDataSource(
            {
                ("public", "customers"): _table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
                ("public", "orders"): _table("public", "orders", [("id", True)]),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        # Entities OK, join broken.
        assert report.entities_healthy == 2
        join_drifts = [d for d in report.drifts if d.def_kind == "canonical_join"]
        assert len(join_drifts) == 1
        assert join_drifts[0].drift_kind == "join_column_missing"
        assert "customer_id" in join_drifts[0].detail


# ----- caching --------------------------------------------------------------


class TestLiveSourceErrorPropagation:
    def test_non_table_not_found_exception_propagates(
        self, store: SQLiteStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        # `TableNotFoundError` is cached as a missing-table signal;
        # any other exception is a real introspection failure and must
        # propagate (with a log line naming the offending (schema,
        # table)) so the CLI's outer translator can return exit 2 with
        # context. Without the re-raise path the operator sees a
        # generic "drift report came back clean" with no clue.
        _seed_customers_table_row(store)
        store.write_entity(
            Entity(
                name="customer",
                description="",
                binding=SingleTableBinding(qualified_table="public.customers"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )

        class _ExplodingSource:
            """get_table raises a non-TableNotFoundError exception."""

            def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
                raise AssertionError("not used")

            def get_table(self, name: str, schema: str) -> Table:
                raise RuntimeError("connection dropped mid-reflection")

            def close(self) -> None:
                pass

        import logging

        with (
            caplog.at_level(logging.ERROR, logger="schemabrain.check.engine"),
            pytest.raises(RuntimeError, match="connection dropped"),
        ):
            check_drift(
                store=store,
                source=_ExplodingSource(),
                source_connection_id=SOURCE_ID,
            )
        # Log line MUST name the (schema, table) so the operator
        # knows which definition triggered the failure.
        assert any("public" in rec.message and "customers" in rec.message for rec in caplog.records)


class TestLiveSourceCaching:
    def test_repeated_table_references_hit_live_source_once(self, store: SQLiteStore) -> None:
        # Many metrics anchored on the same `order` entity: live source
        # should be hit for orders exactly once, not once per metric.
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        for i in range(5):
            store.write_metric(
                Metric(
                    name=f"metric_{i}",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="sum", column="total_cents"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=SOURCE_ID,
            )
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False)],
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.drifts == ()
        assert report.metrics_healthy == 5
        # 1 entity + 5 metrics touched the orders table; engine should
        # have queried it exactly once.
        assert source.calls.count(("public", "orders")) == 1


# ----- report shape ---------------------------------------------------------


class TestReportShape:
    def test_to_json_dict_round_trips(self, store: SQLiteStore) -> None:
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        # Drift: orders.id is gone from live.
        source = FakeDataSource(
            {
                ("public", "orders"): _table("public", "orders", [("total_cents", False)]),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        payload = report.to_json_dict()
        assert set(payload.keys()) == {
            "drifts",
            "summary",
            "exit_code",
        }
        assert payload["exit_code"] == 1
        assert payload["summary"]["entities_total"] == 1
        assert payload["summary"]["entities_healthy"] == 0
        assert isinstance(payload["drifts"], list)
        assert payload["drifts"][0]["drift_kind"] == "identity_column_missing"

    def test_drift_rejects_invalid_def_kind(self) -> None:
        with pytest.raises(ValueError, match="def_kind must be one of"):
            Drift(
                def_kind="cosmic",  # type: ignore[arg-type]
                def_name="x",
                drift_kind="table_missing",
                detail="public.x",
                fix_hint="fix",
            )

    def test_drift_rejects_invalid_drift_kind(self) -> None:
        with pytest.raises(ValueError, match="drift_kind must be one of"):
            Drift(
                def_kind="entity",
                def_name="x",
                drift_kind="cosmic",  # type: ignore[arg-type]
                detail="public.x",
                fix_hint="fix",
            )

    def test_drift_rejects_empty_def_name(self) -> None:
        with pytest.raises(ValueError, match="def_name must be non-empty"):
            Drift(
                def_kind="entity",
                def_name="",
                drift_kind="table_missing",
                detail="public.x",
                fix_hint="fix",
            )

    def test_drift_is_frozen(self) -> None:
        d = Drift(
            def_kind="entity",
            def_name="customer",
            drift_kind="table_missing",
            detail="public.customers",
            fix_hint="restore or rebind",
        )
        with pytest.raises((AttributeError, TypeError)):
            d.def_kind = "metric"  # type: ignore[misc]

    def test_report_with_only_drifts_has_exit_code_one(self, store: SQLiteStore) -> None:
        report = CheckReport(
            drifts=(
                Drift(
                    def_kind="entity",
                    def_name="x",
                    drift_kind="table_missing",
                    detail="public.x",
                    fix_hint="fix",
                ),
            ),
            entities_healthy=0,
            metrics_healthy=0,
            joins_healthy=0,
            total_entities=1,
            total_metrics=0,
            total_joins=0,
        )
        assert report.exit_code == 1

    def test_report_rejects_healthy_greater_than_total(self) -> None:
        # Direct-construction guard: a hand-built report claiming more
        # healthy entities than exist in total is incoherent — refuse.
        with pytest.raises(ValueError, match="entities_healthy"):
            CheckReport(
                drifts=(),
                entities_healthy=5,
                metrics_healthy=0,
                joins_healthy=0,
                total_entities=2,
                total_metrics=0,
                total_joins=0,
            )

    def test_report_rejects_negative_counts(self) -> None:
        with pytest.raises(ValueError, match="joins_healthy"):
            CheckReport(
                drifts=(),
                entities_healthy=0,
                metrics_healthy=0,
                joins_healthy=-1,
                total_entities=0,
                total_metrics=0,
                total_joins=0,
            )

    def test_report_with_zero_definitions_has_exit_code_zero(self) -> None:
        report = CheckReport(
            drifts=(),
            entities_healthy=0,
            metrics_healthy=0,
            joins_healthy=0,
            total_entities=0,
            total_metrics=0,
            total_joins=0,
        )
        assert report.exit_code == 0


class TestColumnShapeDrift:
    """`check` flags columns whose TYPE or NULLability changed since
    index, even when the column still exists. Baseline = the indexed
    snapshot in the store; the live `_table` helper mirrors that snapshot
    by default, so a deliberate `data_types=` / `nullables=` override is
    what injects the drift. Shape drift is additive: it is reported and
    marks the definition not-healthy, but does NOT cascade-suppress
    dependent definitions (the structure is intact)."""

    def test_entity_identity_type_mismatch(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)  # stored id = int
        _seed_customer_entity(store)
        source = FakeDataSource(
            {
                ("public", "customers"): _table(
                    "public",
                    "customers",
                    [("id", True), ("email", False)],
                    data_types={"id": "text"},  # int -> text
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.exit_code == 1
        assert report.entities_healthy == 0  # reported -> not healthy
        drift = next(d for d in report.drifts if d.drift_kind == "type_mismatch")
        assert drift.def_kind == "entity"
        assert "public.customers.id" in drift.detail
        assert "int" in drift.detail and "text" in drift.detail

    def test_entity_identity_nullability_change(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)  # stored id = NOT NULL
        _seed_customer_entity(store)
        source = FakeDataSource(
            {
                ("public", "customers"): _table(
                    "public",
                    "customers",
                    [("id", True), ("email", False)],
                    nullables={"id": True},  # NOT NULL -> nullable
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        drift = next(d for d in report.drifts if d.drift_kind == "nullability_change")
        assert drift.def_kind == "entity"
        assert "public.customers.id" in drift.detail

    def test_shape_drift_does_not_cascade_to_dependent_metric(self, store: SQLiteStore) -> None:
        # An entity's identity-column TYPE change must NOT suppress its
        # metrics' checks (unlike a missing table/column). The entity is
        # reported + not-healthy, but the metric is still evaluated.
        _seed_orders_table_row(store)
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension="order.placed_at",
                time_grains=("day",),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False), ("placed_at", False)],
                    data_types={"id": "text"},  # identity type drift only
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        entity_drifts = [d for d in report.drifts if d.def_kind == "entity"]
        metric_drifts = [d for d in report.drifts if d.def_kind == "metric"]
        assert len(entity_drifts) == 1
        assert entity_drifts[0].drift_kind == "type_mismatch"
        assert metric_drifts == []  # metric evaluated, not suppressed
        assert report.metrics_healthy == 1  # and found healthy

    def test_metric_measure_type_mismatch(self, store: SQLiteStore) -> None:
        _seed_orders_table_row(store)  # stored total_cents = int
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False)],
                    data_types={"total_cents": "text"},  # SUM over text now errors
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.metrics_healthy == 0
        drift = next(d for d in report.drifts if d.def_kind == "metric")
        assert drift.drift_kind == "type_mismatch"
        assert "total_cents" in drift.detail

    def test_metric_time_dimension_nullability_change(self, store: SQLiteStore) -> None:
        _seed_orders_table_row(store)  # stored placed_at = NOT NULL
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension="order.placed_at",
                time_grains=("day",),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False), ("placed_at", False)],
                    nullables={"placed_at": True},  # NOT NULL -> nullable
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        drift = next(d for d in report.drifts if d.drift_kind == "nullability_change")
        assert "placed_at" in drift.detail

    def test_join_column_type_mismatch(self, store: SQLiteStore) -> None:
        _seed_customers_table_row(store)
        _seed_orders_table_row(store)  # stored orders.customer_id = int
        _seed_customer_entity(store)
        _seed_order_entity(store)
        store.write_canonical_join(
            CanonicalJoin(
                name="customer_orders",
                description="",
                source_entity="customer",
                target_entity="order",
                on=(JoinColumnPair(source_column="id", target_column="customer_id"),),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "customers"): _table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("customer_id", False)],
                    data_types={"customer_id": "text"},  # join key type drift
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.joins_healthy == 0
        drift = next(d for d in report.drifts if d.def_kind == "canonical_join")
        assert drift.drift_kind == "type_mismatch"
        assert "customer_id" in drift.detail

    def test_no_stored_baseline_for_column_skips_shape_check(self, store: SQLiteStore) -> None:
        # The indexed snapshot lacks `total_cents` (column added after
        # the last index, metric hand-authored against it). Existence
        # passes against live; with no baseline there is no shape check,
        # so the metric is healthy rather than falsely drifted.
        store.write_table(
            Table(
                schema_name="public",
                name="orders",
                columns=(
                    Column(
                        name="id",
                        table_name="orders",
                        schema_name="public",
                        data_type="int",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        _seed_order_entity(store)
        store.write_metric(
            Metric(
                name="total_revenue",
                description="",
                entity="order",
                measure=MetricMeasure(agg="sum", column="total_cents"),
                time_dimension=None,
                time_grains=(),
            ),
            source_connection_id=SOURCE_ID,
        )
        source = FakeDataSource(
            {
                ("public", "orders"): _table(
                    "public",
                    "orders",
                    [("id", True), ("total_cents", False)],
                    data_types={"total_cents": "numeric"},
                ),
            }
        )
        report = check_drift(store=store, source=source, source_connection_id=SOURCE_ID)
        assert report.drifts == ()
        assert report.metrics_healthy == 1
