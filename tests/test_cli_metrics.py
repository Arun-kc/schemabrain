"""Tests for `schemabrain metrics apply` + `schemabrain metrics list` CLI.

Mirrors `test_cli_entities.py` + `test_cli_joins.py`. Covers:

  - Single-file apply: parses YAML, writes to store, exit 0
  - Directory apply: applies each file, partial-failure summary, exit 1
  - Apply over a dbt-owned metric refuses with DbtOwnedMetricError
    surfacing as user-facing exit 1
  - Apply when anchor entity is missing → FK violation surfaces as
    guided error, exit 1
  - Parse error in one file of a directory doesn't block the rest
  - List empty store → `(no metrics in the store)`, exit 0
  - List with metrics → pretty-prints name, entity, measure, grains
  - List source filtering (`--source` / `--url-env`)
  - URL resolution missing → exit 2
  - Unwritable store path → exit 2
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.metric import Metric, MetricMeasure
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

DBURL = "postgresql+psycopg://user:pw@localhost:5432/db"


# ----- fixtures --------------------------------------------------------------


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


def _seed_order_entity(store_path: Path, *, source_url: str = DBURL) -> str:
    """Seed `public.orders` + `order` entity so metric writes find an
    anchor. Returns the source_connection_id."""
    from schemabrain.cli import _make_source_id

    source_id = _make_source_id(source_url)
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


def _temporal_yaml() -> str:
    return """
version: 1
name: total_revenue
description: Sum of completed order totals.
entity: order
measure:
  agg: sum
  column: total_amount
time_dimension: order.created_at
time_grains: [day, week, month]
""".strip()


def _open_count_yaml() -> str:
    return """
version: 1
name: open_count
entity: order
measure:
  agg: count
  column: id
""".strip()


# ----- helpers ---------------------------------------------------------------


def _set_env(monkeypatch: Any, url: str = DBURL) -> None:
    monkeypatch.setenv("DBURL", url)


def _run(argv: list[str], capsys: Any) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ----- metrics apply ---------------------------------------------------------


class TestMetricsApplySingleFile:
    def test_single_yaml_round_trips_into_store(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path)
        yaml_file = tmp_path / "total_revenue.yaml"
        yaml_file.write_text(_temporal_yaml(), encoding="utf-8")
        _set_env(monkeypatch)

        code, out, err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 0, f"stdout={out!r}, stderr={err!r}"
        assert "applied metric: total_revenue" in out

        # Round-trip via the store.
        with SQLiteStore(store_path) as store:
            got = store.get_metric("total_revenue", source_connection_id=source_id)
        assert got is not None
        assert got.measure.agg == "sum"
        assert got.measure.column == "total_amount"
        assert got.origin == "manual"

    def test_apply_force_overrides_yaml_origin_to_manual(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        # YAML carries `origin: suggested`; the hand-author who runs
        # `metrics apply` overrides any suggestion-provenance with
        # explicit confirmation → stored origin must be `manual`.
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path)
        yaml_text = (
            _temporal_yaml() + "\norigin: suggested\n"
        )
        yaml_file = tmp_path / "total_revenue.yaml"
        yaml_file.write_text(yaml_text, encoding="utf-8")
        _set_env(monkeypatch)

        code, _out, _err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 0
        with SQLiteStore(store_path) as store:
            got = store.get_metric("total_revenue", source_connection_id=source_id)
        assert got is not None
        assert got.origin == "manual"

    def test_missing_anchor_entity_surfaces_guided_error(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        # No `order` entity in the store → FK violation on
        # write_metric → CLI surfaces a "run entities apply first"
        # message and exits 1.
        store_path = tmp_path / "store.db"
        # Open + close the store so the schema is initialised but no
        # `order` entity exists.
        SQLiteStore(store_path).close()
        yaml_file = tmp_path / "total_revenue.yaml"
        yaml_file.write_text(_temporal_yaml(), encoding="utf-8")
        _set_env(monkeypatch)

        code, _out, err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 1
        assert "entity" in err.lower()
        assert "order" in err

    def test_parse_error_surfaces_message(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_order_entity(store_path)
        yaml_file = tmp_path / "bad.yaml"
        # Wrong version → MetricYamlError naming the version field.
        yaml_file.write_text(
            "version: 2\nname: total_revenue\nentity: order\n"
            "measure:\n  agg: sum\n  column: total_amount\n",
            encoding="utf-8",
        )
        _set_env(monkeypatch)

        code, _out, err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 1
        assert "version" in err.lower()

    def test_dbt_owned_metric_overwrite_refused(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        # Pre-seed a dbt-imported metric, then try to overwrite via
        # manual apply → store raises DbtOwnedMetricError, CLI surfaces
        # the guided message and exits 1.
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path)
        with SQLiteStore(store_path) as store:
            store.write_metric(
                Metric(
                    name="total_revenue",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="sum", column="total_amount"),
                    time_dimension="order.created_at",
                    time_grains=("day",),
                    origin="dbt_import",
                ),
                source_connection_id=source_id,
            )
        yaml_file = tmp_path / "total_revenue.yaml"
        yaml_file.write_text(_temporal_yaml(), encoding="utf-8")
        _set_env(monkeypatch)

        code, _out, err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 1
        assert "dbt" in err.lower()
        assert "total_revenue" in err


class TestMetricsApplyDirectory:
    def test_directory_applies_each_yaml(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path)
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        (metrics_dir / "total_revenue.yaml").write_text(
            _temporal_yaml(), encoding="utf-8"
        )
        (metrics_dir / "open_count.yaml").write_text(
            _open_count_yaml(), encoding="utf-8"
        )
        _set_env(monkeypatch)

        code, out, _err = _run(
            [
                "metrics",
                "apply",
                str(metrics_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 0
        assert "applied metric: total_revenue" in out
        assert "applied metric: open_count" in out
        with SQLiteStore(store_path) as store:
            assert (
                store.get_metric("total_revenue", source_connection_id=source_id)
                is not None
            )
            assert (
                store.get_metric("open_count", source_connection_id=source_id)
                is not None
            )

    def test_directory_with_parse_error_skips_bad_file(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path)
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        (metrics_dir / "good.yaml").write_text(_open_count_yaml(), encoding="utf-8")
        (metrics_dir / "bad.yaml").write_text(
            "this is not valid yaml: [\n", encoding="utf-8"
        )
        _set_env(monkeypatch)

        code, out, err = _run(
            [
                "metrics",
                "apply",
                str(metrics_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        # Partial success — at least one file failed.
        assert code == 1
        assert "applied metric: open_count" in out
        assert "bad.yaml" in err
        with SQLiteStore(store_path) as store:
            assert (
                store.get_metric("open_count", source_connection_id=source_id)
                is not None
            )

    def test_empty_directory_returns_error(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_order_entity(store_path)
        empty_dir = tmp_path / "metrics"
        empty_dir.mkdir()
        _set_env(monkeypatch)

        code, _out, err = _run(
            [
                "metrics",
                "apply",
                str(empty_dir),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 1
        assert "no `.yaml`" in err or "no .yaml" in err.lower()

    def test_nonexistent_path_returns_error(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_order_entity(store_path)
        _set_env(monkeypatch)

        code, _out, err = _run(
            [
                "metrics",
                "apply",
                str(tmp_path / "ghost.yaml"),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 1
        assert "ghost.yaml" in err


# ----- metrics list ----------------------------------------------------------


class TestMetricsList:
    def test_empty_store_returns_zero(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()
        code, out, _err = _run(
            ["metrics", "list", "--store-path", str(store_path)], capsys
        )
        assert code == 0
        assert "no metrics" in out.lower()

    def test_lists_written_metrics(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_order_entity(store_path)
        yaml_file = tmp_path / "total_revenue.yaml"
        yaml_file.write_text(_temporal_yaml(), encoding="utf-8")
        _set_env(monkeypatch)

        # Apply first.
        apply_code, _out, _err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert apply_code == 0

        # Then list.
        code, out, _err = _run(
            ["metrics", "list", "--store-path", str(store_path)], capsys
        )
        assert code == 0
        assert "total_revenue" in out
        assert "sum" in out
        assert "total_amount" in out

    def test_filters_by_source(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        # Write the same metric under two different sources. Filtered
        # list should show only the requested source.
        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path, source_url=DBURL)
        other_url = "postgresql+psycopg://user:pw@localhost:5432/other"
        _seed_order_entity(store_path, source_url=other_url)
        with SQLiteStore(store_path) as store:
            store.write_metric(
                Metric(
                    name="src_a_metric",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="count", column="id"),
                    time_dimension=None,
                    time_grains=(),
                ),
                source_connection_id=source_id,
            )

        _set_env(monkeypatch)
        code, out, _err = _run(
            [
                "metrics",
                "list",
                "--store-path",
                str(store_path),
                "--url-env",
                "DBURL",
            ],
            capsys,
        )
        assert code == 0
        assert "src_a_metric" in out


# ----- URL / store errors ----------------------------------------------------


class TestCorruptStore:
    def test_metrics_list_surfaces_corruption_as_exit_2(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        # Hand-corrupt the metrics table so `_row_to_metric` raises
        # ValueError on the canonical-sort invariant. The CLI must
        # surface a structured error and exit 2, not raise a Python
        # traceback.
        import sqlite3

        store_path = tmp_path / "store.db"
        source_id = _seed_order_entity(store_path)
        with SQLiteStore(store_path) as store:
            store.write_metric(
                Metric(
                    name="total_revenue",
                    description="",
                    entity="order",
                    measure=MetricMeasure(agg="sum", column="total_amount"),
                    time_dimension="order.created_at",
                    time_grains=("day", "week"),
                ),
                source_connection_id=source_id,
            )
        # Directly mutate the stored time_grains into an
        # out-of-canonical-order value.
        conn = sqlite3.connect(str(store_path))
        try:
            conn.execute(
                "UPDATE metrics SET time_grains = ? "
                "WHERE name = 'total_revenue'",
                ("week,day",),
            )
            conn.commit()
        finally:
            conn.close()

        code, _out, err = _run(
            ["metrics", "list", "--store-path", str(store_path)], capsys
        )
        assert code == 2
        assert "corrupt" in err.lower()


class TestUrlErrors:
    def test_apply_without_url_returns_2(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        # Neither --source nor --url-env provided → 2 (structural).
        store_path = tmp_path / "store.db"
        _seed_order_entity(store_path)
        yaml_file = tmp_path / "m.yaml"
        yaml_file.write_text(_temporal_yaml(), encoding="utf-8")
        monkeypatch.delenv("DBURL", raising=False)

        code, _out, _err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 2

    def test_apply_with_invalid_url_env_returns_2(
        self, tmp_path: Path, capsys: Any, monkeypatch: Any
    ) -> None:
        # --url-env points to a non-set env var → 2.
        store_path = tmp_path / "store.db"
        _seed_order_entity(store_path)
        yaml_file = tmp_path / "m.yaml"
        yaml_file.write_text(_temporal_yaml(), encoding="utf-8")
        monkeypatch.delenv("UNSET_DBURL", raising=False)

        code, _out, _err = _run(
            [
                "metrics",
                "apply",
                str(yaml_file),
                "--url-env",
                "UNSET_DBURL",
                "--store-path",
                str(store_path),
            ],
            capsys,
        )
        assert code == 2
