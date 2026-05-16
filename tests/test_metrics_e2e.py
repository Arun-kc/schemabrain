"""End-to-end tests for the metric arc.

Two layers:

  - Bundled-fixture shape: every bundled metric YAML parses cleanly,
    anchors on a defined entity, and references a column that exists
    on the ecommerce.sql `orders` table. Plus cardinality is now
    populated on every bundled canonical-join YAML (the resolver's
    fan-out detection consumes it).

  - CLI round-trip: `metrics apply` the bundled directory after
    seeding the prerequisite entities + joins + indexed tables →
    `metrics list` shows all 3 → resolve_metric_plan against the
    store reaches all the way through to a runnable parameterised SQL.

Postgres-backed live execution is exercised by the manual
`scripts/smoke_postgres.py` job — the unit-test layer pins the
contract; the smoke job pins the wire-level behaviour.
"""

from __future__ import annotations

from pathlib import Path

from schemabrain.cli import _make_source_id, main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.join import CanonicalJoin, JoinColumnPair
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.eval.bundled import bundled_metrics_fixture_dir
from schemabrain.metrics.yaml_grammar import parse_metric_yaml_file
from schemabrain.semantic.compiler import (
    emit_sql,
    resolve_metric_plan,
)

_URL = "postgresql+psycopg://u:p@h/db"
_EXPECTED_METRIC_NAMES = frozenset({"total_revenue", "order_count", "customer_count"})


# ----- bundled-fixture structure --------------------------------------------


class TestBundledMetricFixtures:
    def test_fixture_dir_exists(self) -> None:
        path = bundled_metrics_fixture_dir()
        assert path.is_dir()

    def test_all_three_metric_yamls_present_and_parse(self) -> None:
        path = bundled_metrics_fixture_dir()
        yaml_files = sorted(p for p in path.iterdir() if p.suffix == ".yaml")
        names = {parse_metric_yaml_file(p).name for p in yaml_files}
        assert names == _EXPECTED_METRIC_NAMES

    def test_all_metrics_anchor_on_order(self) -> None:
        # Every bundled metric is anchored on `order` so the demo
        # narrative stays tight (one entity, three measures of it).
        path = bundled_metrics_fixture_dir()
        for yaml_file in path.iterdir():
            if yaml_file.suffix != ".yaml":
                continue  # pragma: no cover — only YAMLs in fixture dir
            metric = parse_metric_yaml_file(yaml_file)
            assert metric.entity == "order"

    def test_total_revenue_measures_total_cents(self) -> None:
        # Lock the measure column to the schema in ecommerce.sql —
        # if someone renames `total_cents` upstream, this test catches
        # the drift.
        path = bundled_metrics_fixture_dir()
        metric = parse_metric_yaml_file(path / "total_revenue.yaml")
        assert metric.measure.agg == "sum"
        assert metric.measure.column == "total_cents"

    def test_metrics_use_placed_at_time_dimension(self) -> None:
        # Same column-name lock as above: `placed_at` is the orders
        # table's actual timestamp column.
        path = bundled_metrics_fixture_dir()
        for name in ("total_revenue", "order_count"):
            metric = parse_metric_yaml_file(path / f"{name}.yaml")
            assert metric.time_dimension == "order.placed_at"
            assert "day" in metric.time_grains

    def test_customer_count_uses_distinct_user_id(self) -> None:
        path = bundled_metrics_fixture_dir()
        metric = parse_metric_yaml_file(path / "customer_count.yaml")
        assert metric.measure.agg == "count_distinct"
        assert metric.measure.column == "user_id"


class TestBundledJoinCardinality:
    """The cardinality column on `canonical_joins` is consumed by the
    compiler for fan-out detection. The bundled join fixtures need
    their cardinality populated so the demo narrative shows
    cardinality-aware behaviour out of the box."""

    def test_all_bundled_joins_carry_cardinality(self) -> None:
        from schemabrain.eval.bundled import bundled_joins_fixture_dir
        from schemabrain.joins.yaml_grammar import parse_canonical_join_yaml_file

        path = bundled_joins_fixture_dir()
        for yaml_file in path.iterdir():
            if yaml_file.suffix != ".yaml":
                continue  # pragma: no cover — only YAMLs in fixture dir
            join = parse_canonical_join_yaml_file(yaml_file)
            assert join.cardinality is not None, f"{yaml_file.name} is missing cardinality"


# ----- CLI round-trip + compiler reach --------------------------------------


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
                name="user_id",
                table_name="orders",
                schema_name="public",
                data_type="bigint",
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


def _seed_order_entity_for_url(store_path: Path) -> str:
    source_id = _make_source_id(_URL)
    with SQLiteStore(store_path) as store:
        store.write_table(_orders_table(), source_connection_id=source_id)
        store.write_entity(
            Entity(
                name="order",
                description="",
                binding=SingleTableBinding(qualified_table="public.orders"),
                identity="id",
            ),
            source_connection_id=source_id,
        )
    return source_id


class TestCliRoundTrip:
    def test_apply_bundled_metrics_lands_all_three(self, tmp_path: Path, monkeypatch) -> None:
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity_for_url(store_path)
        monkeypatch.setenv("DBURL", _URL)
        fixture_dir = bundled_metrics_fixture_dir()

        exit_code = main(
            [
                "metrics",
                "apply",
                str(fixture_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0

        with SQLiteStore(store_path) as store:
            names = {m.name for m in store.list_metrics(source_connection_id=source_id)}
        assert names == _EXPECTED_METRIC_NAMES

    def test_compiler_reaches_all_three_to_emit_sql(self, tmp_path: Path, monkeypatch) -> None:
        # After apply, the compiler can resolve + emit SQL for each
        # bundled metric. This pins the metric-fixture / compiler /
        # store integration in one go.
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity_for_url(store_path)
        monkeypatch.setenv("DBURL", _URL)
        fixture_dir = bundled_metrics_fixture_dir()

        main(
            [
                "metrics",
                "apply",
                str(fixture_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ]
        )

        with SQLiteStore(store_path) as store:
            for name in _EXPECTED_METRIC_NAMES:
                plan = resolve_metric_plan(
                    store=store,
                    source_connection_id=source_id,
                    metric_name=name,
                    time_grain="month" if name != "customer_count" else "week",
                )
                sql, params = emit_sql(plan)
                assert ";" not in sql
                assert "LIMIT :p_limit" in sql
                # `p_limit` is always bound — exercises the params dict
                assert "p_limit" in params

    def test_metrics_list_after_apply(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # The verification path after `metrics apply`. The list
        # output must include each metric's name + measure shape.
        store_path = tmp_path / "store.db"
        _seed_order_entity_for_url(store_path)
        monkeypatch.setenv("DBURL", _URL)
        fixture_dir = bundled_metrics_fixture_dir()

        main(
            [
                "metrics",
                "apply",
                str(fixture_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ]
        )
        capsys.readouterr()  # drop the apply output

        exit_code = main(["metrics", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        for name in _EXPECTED_METRIC_NAMES:
            assert name in out


# ----- multi-canonical-per-pair → ambiguity refusal at metric layer ---------


class TestAmbiguityRefusalE2E:
    def test_billing_shipping_ambiguity_surfaces_via_get_metric(self, tmp_path: Path) -> None:
        # The bundled fixture's billing/shipping joins create the
        # classic ambiguous-join case. Apply both joins + the
        # total_revenue metric, then ask the compiler to group by
        # `address.country` — it must raise AmbiguousJoinError
        # carrying both candidate names.
        from schemabrain.semantic.compiler import (
            AmbiguousJoinError as CompilerAmbiguousJoinError,
        )

        store_path = tmp_path / "store.db"
        source_id = _make_source_id(_URL)
        with SQLiteStore(store_path) as store:
            store.write_table(_orders_table(), source_connection_id=source_id)
            store.write_table(
                Table(
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
                    foreign_keys=(),
                ),
                source_connection_id=source_id,
            )
            store.write_entity(
                Entity(
                    name="order",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.orders"),
                    identity="id",
                ),
                source_connection_id=source_id,
            )
            store.write_entity(
                Entity(
                    name="address",
                    description="",
                    binding=SingleTableBinding(qualified_table="public.addresses"),
                    identity="id",
                ),
                source_connection_id=source_id,
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
                    source_connection_id=source_id,
                )
            # Apply the bundled total_revenue metric.
            fixture_dir = bundled_metrics_fixture_dir()
            metric = parse_metric_yaml_file(fixture_dir / "total_revenue.yaml")
            store.write_metric(metric, source_connection_id=source_id)

            # Now ask the compiler to slice revenue by address.country —
            # this requires resolving order ↔ address, which is
            # ambiguous between billing and shipping.
            import pytest

            with pytest.raises(CompilerAmbiguousJoinError) as exc_info:
                resolve_metric_plan(
                    store=store,
                    source_connection_id=source_id,
                    metric_name="total_revenue",
                    group_by=("address.country",),
                )
        assert set(exc_info.value.candidate_join_names) == {
            "order_billing_address",
            "order_shipping_address",
        }
