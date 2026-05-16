"""Tests for `schemabrain import dbt --include-metrics`.

Verifies the integration between the entity-import flow and the
metric-import sidecar: a manifest carrying both `nodes` (entities) and
`metrics` + `semantic_models` (metrics) imports both in one CLI
invocation, and metrics anchor cleanly on the entities the same run
just wrote.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from schemabrain.cli import _cmd_import_dbt, _make_source_id
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

_SOURCE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"


# ----- fakes -----------------------------------------------------------------


class _FakeDataSource:
    def __init__(self, tables: Mapping[tuple[str, str], Table]) -> None:
        self._tables = dict(tables)

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        return list(self._tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        try:
            return self._tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"table {schema}.{name} not found") from exc

    def close(self) -> None:  # pragma: no cover — never closed
        pass

    def __enter__(self) -> _FakeDataSource:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


def _factory_for(source: _FakeDataSource):
    def factory(url: str) -> _FakeDataSource:
        return source

    return factory


def _orders_live_source() -> _FakeDataSource:
    orders = Table(
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
                name="total_amount",
                table_name="orders",
                schema_name="public",
                data_type="numeric",
                nullable=False,
                ordinal_position=2,
                is_primary_key=False,
            ),
            Column(
                name="ordered_at",
                table_name="orders",
                schema_name="public",
                data_type="timestamp",
                nullable=False,
                ordinal_position=3,
                is_primary_key=False,
            ),
        ),
        foreign_keys=(),
    )
    return _FakeDataSource({("public", "orders"): orders})


def _seed_orders_table(tmp_path: Path) -> Path:
    store_path = tmp_path / "store.db"
    source_id = _make_source_id(_SOURCE_URL)
    with SQLiteStore(store_path) as store:
        table = Table(
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
                    name="total_amount",
                    table_name="orders",
                    schema_name="public",
                    data_type="numeric",
                    nullable=False,
                    ordinal_position=2,
                    is_primary_key=False,
                ),
                Column(
                    name="ordered_at",
                    table_name="orders",
                    schema_name="public",
                    data_type="timestamp",
                    nullable=False,
                    ordinal_position=3,
                    is_primary_key=False,
                ),
            ),
            foreign_keys=(),
        )
        store.write_table(table, source_connection_id=source_id)
    return store_path


def _manifest_with_entity_and_metric(tmp_path: Path) -> Path:
    """Build a minimal manifest carrying one model (entity) + one
    simple metric anchored on it."""
    manifest = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "project_name": "demo",
            "dbt_version": "1.7.0",
        },
        "nodes": {
            "model.demo.order": {
                "resource_type": "model",
                "name": "order",
                "package_name": "demo",
                "path": "models/order.sql",
                "original_file_path": "models/order.sql",
                "unique_id": "model.demo.order",
                "fqn": ["demo", "order"],
                "alias": "orders",
                "database": "demo_db",
                "schema": "public",
                "description": "One row per order.",
                "config": {"materialized": "table"},
                "constraints": [],
                "columns": {
                    "id": {
                        "name": "id",
                        "data_type": "bigint",
                        "constraints": [{"type": "primary_key"}],
                    },
                    "total_amount": {
                        "name": "total_amount",
                        "data_type": "numeric",
                        "constraints": [],
                    },
                    "ordered_at": {
                        "name": "ordered_at",
                        "data_type": "timestamp",
                        "constraints": [],
                    },
                },
                "depends_on": {"nodes": []},
            }
        },
        "sources": {},
        "metrics": {
            "metric.demo.total_revenue": {
                "resource_type": "metric",
                "name": "total_revenue",
                "label": "Total Revenue",
                "description": "Sum of completed order totals.",
                "type": "simple",
                "type_params": {"measure": {"name": "revenue_measure"}},
                "filter": None,
            }
        },
        "semantic_models": {
            "semantic_model.demo.orders": {
                "resource_type": "semantic_model",
                "name": "orders",
                "node_relation": {"schema_name": "public", "alias": "orders"},
                "measures": [
                    {
                        "name": "revenue_measure",
                        "agg": "sum",
                        "expr": "total_amount",
                    }
                ],
                "entities": [{"name": "order", "type": "primary", "expr": "id"}],
                "dimensions": [
                    {
                        "name": "ordered_at",
                        "type": "time",
                        "type_params": {"time_granularity": "day"},
                    }
                ],
            }
        },
    }
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    return target


# ----- the test --------------------------------------------------------------


class TestIncludeMetrics:
    def test_imports_metric_alongside_entity(self, tmp_path: Path, capsys: Any) -> None:
        store_path = _seed_orders_table(tmp_path)
        manifest_path = _manifest_with_entity_and_metric(tmp_path)

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            include_metrics=True,
            _source_factory=_factory_for(_orders_live_source()),
        )
        assert exit_code == 0

        source_id = _make_source_id(_SOURCE_URL)
        with SQLiteStore(store_path) as store:
            entity = store.get_entity("order", source_connection_id=source_id)
            metric = store.get_metric("total_revenue", source_connection_id=source_id)

        # Entity import wrote with origin=dbt_import.
        assert entity is not None
        assert entity.origin == "dbt_import"

        # Metric import wrote with origin=dbt_import, anchored on the
        # imported entity, with the correct measure shape.
        assert metric is not None
        assert metric.origin == "dbt_import"
        assert metric.entity == "order"
        assert metric.measure.agg == "sum"
        assert metric.measure.column == "total_amount"
        assert metric.time_dimension == "order.ordered_at"
        assert metric.time_grains == ("day",)

        out = capsys.readouterr().out
        assert "dbt metrics: imported 1, skipped 0" in out

    def test_include_metrics_off_by_default(self, tmp_path: Path, capsys: Any) -> None:
        # Without --include-metrics, the metric portion of the manifest
        # is ignored entirely (no skip count printed).
        store_path = _seed_orders_table(tmp_path)
        manifest_path = _manifest_with_entity_and_metric(tmp_path)

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            include_metrics=False,
            _source_factory=_factory_for(_orders_live_source()),
        )
        assert exit_code == 0

        source_id = _make_source_id(_SOURCE_URL)
        with SQLiteStore(store_path) as store:
            metric = store.get_metric("total_revenue", source_connection_id=source_id)
        # Metric was NOT imported.
        assert metric is None

        out = capsys.readouterr().out
        assert "dbt metrics:" not in out

    def test_dry_run_does_not_write(self, tmp_path: Path, capsys: Any) -> None:
        store_path = _seed_orders_table(tmp_path)
        manifest_path = _manifest_with_entity_and_metric(tmp_path)

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=True,
            report_path=None,
            include_metrics=True,
            _source_factory=_factory_for(_orders_live_source()),
        )
        assert exit_code == 0

        source_id = _make_source_id(_SOURCE_URL)
        with SQLiteStore(store_path) as store:
            metric = store.get_metric("total_revenue", source_connection_id=source_id)
        assert metric is None

        out = capsys.readouterr().out
        # Dry-run uses "would import" instead of "imported".
        assert "dbt metrics: would import 1" in out

    def test_metric_skipped_when_anchor_entity_not_imported(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # Build a manifest whose metric anchors on `order` but whose
        # only entity model is `customer` — the order entity isn't in
        # the imported set, so the metric skips with
        # `anchor_entity_not_imported`.
        store_path = _seed_orders_table(tmp_path)
        manifest = json.loads(_manifest_with_entity_and_metric(tmp_path).read_text())
        # Rename the model from `order` to `customer` so the entity
        # import lands a `customer` entity but the metric (still
        # anchored on `order`) can't find its anchor.
        manifest["nodes"]["model.demo.customer"] = manifest["nodes"].pop("model.demo.order")
        manifest["nodes"]["model.demo.customer"]["name"] = "customer"
        manifest["nodes"]["model.demo.customer"]["unique_id"] = "model.demo.customer"
        manifest_path = tmp_path / "manifest2.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        # Also need a live `customer` table (the entity importer
        # verifies against the live source).
        customer_source = _FakeDataSource(
            {
                ("public", "orders"): Table(
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
                            name="total_amount",
                            table_name="orders",
                            schema_name="public",
                            data_type="numeric",
                            nullable=False,
                            ordinal_position=2,
                            is_primary_key=False,
                        ),
                        Column(
                            name="ordered_at",
                            table_name="orders",
                            schema_name="public",
                            data_type="timestamp",
                            nullable=False,
                            ordinal_position=3,
                            is_primary_key=False,
                        ),
                    ),
                    foreign_keys=(),
                ),
            }
        )

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest_path),
            positional_url=_SOURCE_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            include_metrics=True,
            _source_factory=_factory_for(customer_source),
        )
        assert exit_code == 0

        out = capsys.readouterr().out
        assert "dbt metrics: imported 0, skipped 1" in out
        assert "anchor_entity_not_imported" in out
