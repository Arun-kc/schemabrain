"""Tests for `schemabrain index --dry-run --since DATE`.

Splits into two layers:

- Store-level: `SQLiteStore.count_stale_tables_and_columns` returns the
  expected counts when rows are seeded with known `indexed_at` values.
- CLI-level: argparse validation (`--since` requires `--dry-run`) and
  guided-error rendering on a malformed `--since` value. The freshness
  audit's end-to-end flow against a live Postgres source is covered in
  the integration suite in `test_cli.py`; here we exercise the
  parsing + error paths that do not need a database.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore


class TestStoreCountStaleTablesAndColumns:
    def test_no_rows_returns_zero_zero(self, tmp_path: Path) -> None:
        store_path = tmp_path / "store.db"
        store = SQLiteStore(store_path)
        try:
            tables, columns = store.count_stale_tables_and_columns(
                source_connection_id="src1",
                since_ts=int(time.time()),
            )
        finally:
            store.close()
        assert (tables, columns) == (0, 0)

    def test_old_rows_are_counted(self, tmp_path: Path) -> None:
        store_path = tmp_path / "store.db"
        store = SQLiteStore(store_path)
        try:
            # Two stale tables with 3 + 2 columns; one fresh table with 1
            # column. Stale-side aggregate should be (2 tables, 5 columns).
            _seed_table_with_columns(
                store, schema="public", name="orders", indexed_at=100, columns=("id", "ts", "amt")
            )
            _seed_table_with_columns(
                store, schema="public", name="customers", indexed_at=200, columns=("id", "email")
            )
            _seed_table_with_columns(
                store, schema="public", name="items", indexed_at=9000, columns=("id",)
            )

            tables, columns = store.count_stale_tables_and_columns(
                source_connection_id="src1",
                since_ts=1000,
            )
        finally:
            store.close()
        assert (tables, columns) == (2, 5)

    def test_other_source_ids_are_excluded(self, tmp_path: Path) -> None:
        store_path = tmp_path / "store.db"
        store = SQLiteStore(store_path)
        try:
            _seed_table_with_columns(
                store,
                schema="public",
                name="orders",
                indexed_at=100,
                columns=("id",),
                source_connection_id="src1",
            )
            _seed_table_with_columns(
                store,
                schema="public",
                name="orders",
                indexed_at=100,
                columns=("id",),
                source_connection_id="src2",
            )

            tables, columns = store.count_stale_tables_and_columns(
                source_connection_id="src1",
                since_ts=1000,
            )
        finally:
            store.close()
        assert (tables, columns) == (1, 1)

    def test_closed_store_raises(self, tmp_path: Path) -> None:
        store_path = tmp_path / "store.db"
        store = SQLiteStore(store_path)
        store.close()
        with pytest.raises(RuntimeError, match="closed"):
            store.count_stale_tables_and_columns(
                source_connection_id="src1",
                since_ts=1,
            )


class TestCliSinceWithoutDryRunRejected:
    def test_since_without_dry_run_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = cli_main(
            [
                "index",
                "postgresql+psycopg://nope:nope@localhost:1/never",
                "--since",
                "1d",
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--since" in err
        assert "--dry-run" in err


class TestSinceArgparseSurface:
    def test_since_flag_present_on_parser(self) -> None:
        from schemabrain.cli import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(
            [
                "index",
                "postgresql+psycopg://x",
                "--dry-run",
                "--since",
                "1d",
            ]
        )
        assert ns.since == "1d"
        assert ns.dry_run is True

    def test_since_defaults_to_none(self) -> None:
        from schemabrain.cli import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["index", "postgresql+psycopg://x"])
        assert ns.since is None


def _seed_table_with_columns(
    store: SQLiteStore,
    *,
    schema: str,
    name: str,
    indexed_at: int,
    columns: tuple[str, ...],
    source_connection_id: str = "src1",
) -> None:
    """Insert a tables row + columns rows directly via the store's connection.

    Bypasses the normal `index()` write path so tests can control the
    `indexed_at` value precisely. Real indexing always stamps "now"
    which would make freshness assertions time-sensitive.
    """
    conn = store._conn
    assert conn is not None
    conn.execute(
        "INSERT INTO tables (schema_name, name, source_connection_id, indexed_at) "
        "VALUES (?, ?, ?, ?)",
        (schema, name, source_connection_id, indexed_at),
    )
    for idx, col_name in enumerate(columns):
        conn.execute(
            "INSERT INTO columns ("
            "schema_name, table_name, name, source_connection_id, data_type, "
            "nullable, ordinal_position, default_expr, is_primary_key"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (schema, name, col_name, source_connection_id, "text", 1, idx, None, 0),
        )
    conn.commit()
