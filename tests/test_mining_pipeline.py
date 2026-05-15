"""Tests for the query log mining pipeline.

`mine_queries` is the orchestration function: fetch from
`pg_stat_statements`, parse each statement with `sqlglot`, resolve
table references against the indexed-tables set, and UPSERT into
`example_queries`.

Tests inject a fake `fetch_statements` callable so the pipeline can
be exercised without a real Postgres — the reader's own behaviour
(soft-skip on extension unavailable) is tested separately in
`test_mining_pg_stat_statements.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.store import SQLiteStore
from schemabrain.mining.pg_stat_statements import PgStatRow
from schemabrain.mining.pipeline import MineReport, mine_queries
from tests._example_query_helpers import seed_table_for_examples as _seed_table


def _fake_fetch(rows: list[PgStatRow]):
    """Return a `fetch_statements` callable that yields the given rows."""

    def _fetch(_engine: object) -> list[PgStatRow]:
        return rows

    return _fetch


def _unavailable_fetch(_engine: object) -> list[PgStatRow]:
    """Reader returns the sentinel empty list and the pipeline treats it
    as unavailable when the reader also flags the source — but the
    pipeline doesn't actually need a sentinel; it observes through
    `len(rows) == 0` whether anything was mined. The
    `skipped_unavailable` flag is set by the READER and surfaces in
    the report when the reader couldn't connect. Tests of that flag
    use a fetch that raises a known sentinel.
    """
    return []


class TestEmptyInput:
    def test_no_statements_returns_zero_report(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([]),
            )
            assert report == MineReport(
                statements_read=0,
                statements_used=0,
                rows_written=0,
                skipped_unavailable=False,
            )


class TestSingleTable:
    def test_select_from_one_indexed_table_writes_one_row(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch(
                    [PgStatRow(query="SELECT id FROM public.orders", calls=10)]
                ),
            )
            assert report.statements_read == 1
            assert report.statements_used == 1
            assert report.rows_written == 1
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].sql_text == "SELECT id FROM public.orders"
            assert stored[0].observation_count == 10
            assert stored[0].sensitivity == "public"
            assert stored[0].pii_categories == frozenset()


class TestMultipleTables:
    def test_join_writes_one_row_per_touched_table(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="public", table="users")
            join_sql = "SELECT * FROM public.orders o JOIN public.users u ON u.id = o.user_id"
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query=join_sql, calls=5)]),
            )
            assert report.statements_read == 1
            assert report.statements_used == 1
            assert report.rows_written == 2  # one row per touched table

            orders_rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            users_rows = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert len(orders_rows) == 1
            assert orders_rows[0].sql_text == join_sql
            assert len(users_rows) == 1
            assert users_rows[0].sql_text == join_sql


class TestUnqualifiedResolution:
    def test_unqualified_table_resolves_when_single_indexed_match(self, tmp_path: Path) -> None:
        # Only `public.orders` is indexed; an unqualified `FROM orders`
        # resolves to it.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query="SELECT id FROM orders", calls=3)]),
            )
            assert report.rows_written == 1
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1

    def test_unqualified_ambiguous_skips_statement(self, tmp_path: Path) -> None:
        # Two indexed schemas both have `orders`; unqualified
        # reference is ambiguous and skipped.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="staging", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query="SELECT id FROM orders", calls=3)]),
            )
            assert report.statements_read == 1
            assert report.statements_used == 0
            assert report.rows_written == 0


class TestFilteringOutNonIndexedTables:
    def test_statement_touching_no_indexed_table_is_skipped(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch(
                    [PgStatRow(query="SELECT * FROM pg_class", calls=100)]
                ),
            )
            assert report.statements_read == 1
            assert report.statements_used == 0
            assert report.rows_written == 0

    def test_statement_touching_some_indexed_and_some_not_uses_only_indexed(
        self, tmp_path: Path
    ) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            # Joins orders (indexed) with pg_class (not indexed). Only
            # orders gets an example_queries row.
            sql = "SELECT o.* FROM public.orders o JOIN pg_class c ON c.relname = 'orders'"
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query=sql, calls=1)]),
            )
            assert report.rows_written == 1

    def test_qualified_ref_to_non_indexed_schema_is_skipped(self, tmp_path: Path) -> None:
        # `pg_catalog.pg_class` is qualified but NOT in the indexed-
        # tables set. Combined with one ref that IS indexed, this
        # statement has multiple iterations through `_resolve_references`
        # — exercising the False branch of the qualified-match check
        # while another iteration still pending.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            sql = (
                "SELECT o.id FROM public.orders o "
                "JOIN pg_catalog.pg_class c ON c.relname = o.id::text"
            )
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query=sql, calls=1)]),
            )
            # Only the indexed orders ref produces a row.
            assert report.rows_written == 1


class TestFilteringOutNonDml:
    @pytest.mark.parametrize(
        "ddl",
        [
            "CREATE TABLE bar (id INT)",
            "DROP TABLE bar",
            "BEGIN",
            "COMMIT",
            "SET search_path = public",
        ],
    )
    def test_ddl_and_control_statements_skipped(self, tmp_path: Path, ddl: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query=ddl, calls=1)]),
            )
            assert report.statements_read == 1
            assert report.statements_used == 0
            assert report.rows_written == 0


class TestParseFailureIsolation:
    def test_one_unparseable_statement_does_not_blow_up_batch(self, tmp_path: Path) -> None:
        # A single garbage row from pg_stat_statements must not fail
        # the batch — the well-formed row alongside it lands cleanly.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch(
                    [
                        PgStatRow(query="not even close ###", calls=1),
                        PgStatRow(query="SELECT id FROM public.orders", calls=5),
                    ]
                ),
            )
            assert report.statements_read == 2
            assert report.rows_written == 1


class TestUpsertSemantics:
    def test_re_mining_preserves_first_seen_at_and_updates_count(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            sql = "SELECT id FROM public.orders"

            mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query=sql, calls=10)]),
                now_unix=1_700_000_000,
            )
            first_state = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )[0]
            assert first_state.observation_count == 10
            assert first_state.first_seen_at == 1_700_000_000

            # Second mining run: same SQL, higher count, later
            # timestamp. UPSERT preserves first_seen_at.
            mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_fake_fetch([PgStatRow(query=sql, calls=42)]),
                now_unix=1_700_100_000,
            )
            second_state = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )[0]
            assert second_state.observation_count == 42
            assert second_state.first_seen_at == 1_700_000_000  # preserved
            assert second_state.last_seen_at == 1_700_100_000  # updated


class TestUnavailableSource:
    def test_reader_unavailable_returns_skipped_report(self, tmp_path: Path) -> None:
        # The reader signals unavailability by raising a known
        # sentinel exception — the pipeline catches it and returns a
        # report with skipped_unavailable=True. No example_queries
        # rows are written.
        from schemabrain.mining.pg_stat_statements import (
            PgStatStatementsUnavailable,
        )

        def _unavail(_engine: object) -> list[PgStatRow]:
            raise PgStatStatementsUnavailable("extension not installed")

        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            report = mine_queries(
                engine=None,
                store=store,
                source_connection_id="src",
                fetch_statements=_unavail,
            )
            assert report.skipped_unavailable is True
            assert report.statements_read == 0
            assert report.statements_used == 0
            assert report.rows_written == 0
            assert (
                store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=10,
                )
                == []
            )
