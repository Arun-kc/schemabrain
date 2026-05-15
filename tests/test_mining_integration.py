"""End-to-end integration test for query log mining.

Boots a Postgres container with `pg_stat_statements` loaded, runs a
known set of statements against it, then calls the mining pipeline
against the live view and verifies that the expected `example_queries`
rows land in the local store.

Marked `integration` so contributors without Docker can skip with
`-m "not integration"`. The shared `_pg_container` fixture in
`conftest.py` doesn't preload the extension — this test spins up its
own container.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from testcontainers.postgres import PostgresContainer

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.mining.pipeline import mine_queries

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def pgss_url() -> Iterator[str]:
    """Postgres with `pg_stat_statements` loaded + seeded with known statements.

    The container is configured by passing `-c shared_preload_libraries
    =pg_stat_statements` to the postgres command — that's the only way
    the extension's hooks engage at startup. `CREATE EXTENSION` is then
    a routine catalog-level call inside the test setup.

    Schema seeded:
      public.orders (id INT, total INT)
      public.users  (id INT, email TEXT)

    A handful of representative statements run against the seeded
    tables so `pg_stat_statements` has real rows to mine.
    """
    container = PostgresContainer("postgres:16-alpine", driver="psycopg")
    container.with_command("postgres -c shared_preload_libraries=pg_stat_statements")
    container.start()
    try:
        url = container.get_connection_url()
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION pg_stat_statements"))
                conn.execute(text("CREATE TABLE orders (id INT PRIMARY KEY, total INT NOT NULL)"))
                conn.execute(text("CREATE TABLE users (id INT PRIMARY KEY, email TEXT NOT NULL)"))
                conn.execute(text("INSERT INTO orders VALUES (1, 100), (2, 250)"))
                conn.execute(text("INSERT INTO users VALUES (1, 'a@x'), (2, 'b@x')"))
                # Reset so the seed-time DDL/inserts don't dominate the
                # view; we want the mining-target queries to be the
                # most-called rows.
                conn.execute(text("SELECT pg_stat_statements_reset()"))
            # Now run the "real" queries the mining pass will harvest.
            with engine.begin() as conn:
                # SELECT against one indexed table:
                for _ in range(5):
                    conn.execute(text("SELECT id, total FROM public.orders"))
                # JOIN across two indexed tables:
                for _ in range(3):
                    conn.execute(
                        text(
                            "SELECT o.id, u.email "
                            "FROM public.orders o "
                            "JOIN public.users u ON u.id = o.id"
                        )
                    )
                # Query against a non-indexed table (pg_class) — this
                # should NOT produce a row in example_queries.
                conn.execute(text("SELECT relname FROM pg_class LIMIT 5"))
                # DDL — must NOT produce a row.
                conn.execute(text("CREATE TABLE _drop_me (x INT)"))
                conn.execute(text("DROP TABLE _drop_me"))
        finally:
            engine.dispose()
        yield url
    finally:
        container.stop()


def _seed_indexed_tables(store: SQLiteStore, *, source_id: str) -> None:
    """Register `public.orders` and `public.users` in the local store
    so the mining pipeline knows they're indexed.
    """
    for table_name in ("orders", "users"):
        col = Column(
            name="id",
            table_name=table_name,
            schema_name="public",
            data_type="INTEGER",
            nullable=False,
            ordinal_position=1,
        )
        store.write_table(
            Table(name=table_name, schema_name="public", columns=(col,)),
            source_connection_id=source_id,
        )


class TestEndToEnd:
    def test_mining_writes_expected_rows(self, pgss_url: str, tmp_path: Path) -> None:
        engine = create_engine(pgss_url)
        try:
            with SQLiteStore(tmp_path / "s.db") as store:
                _seed_indexed_tables(store, source_id="src")
                report = mine_queries(
                    engine=engine,
                    store=store,
                    source_connection_id="src",
                )
                # The reader saw multiple statements; at least the SELECT
                # and the JOIN are above 0 calls and should produce rows.
                assert report.statements_read > 0
                assert report.statements_used >= 2  # SELECT + JOIN
                assert report.rows_written >= 3  # 1 (orders) + 2 (orders, users)
                assert report.skipped_unavailable is False

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
                # `public.orders` appears in both the SELECT and the
                # JOIN; `public.users` appears only in the JOIN. Both
                # should have at least one row.
                assert len(orders_rows) >= 1
                assert len(users_rows) >= 1
                # No rows should reference non-indexed tables:
                conn = store._require_conn()
                all_tables = conn.execute(
                    "SELECT DISTINCT schema_name, table_name "
                    "FROM example_queries WHERE source_connection_id = ?",
                    ("src",),
                ).fetchall()
                table_set = {(r["schema_name"], r["table_name"]) for r in all_tables}
                assert table_set <= {("public", "orders"), ("public", "users")}
        finally:
            engine.dispose()

    def test_re_mining_preserves_first_seen_at(self, pgss_url: str, tmp_path: Path) -> None:
        engine = create_engine(pgss_url)
        try:
            with SQLiteStore(tmp_path / "s.db") as store:
                _seed_indexed_tables(store, source_id="src")
                # First pass at one timestamp:
                mine_queries(
                    engine=engine,
                    store=store,
                    source_connection_id="src",
                    now_unix=1_700_000_000,
                )
                first_state = store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=10,
                )
                first_seen_values = {r.first_seen_at for r in first_state}
                # Second pass at a later timestamp:
                mine_queries(
                    engine=engine,
                    store=store,
                    source_connection_id="src",
                    now_unix=1_700_100_000,
                )
                second_state = store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=10,
                )
                # `first_seen_at` on every row that survived from the
                # first pass must be the FIRST timestamp, not the second.
                for row in second_state:
                    assert row.first_seen_at == 1_700_000_000
                    # And `last_seen_at` must be the SECOND.
                    assert row.last_seen_at == 1_700_100_000
                # Sanity check on the test setup itself:
                assert 1_700_000_000 in first_seen_values
        finally:
            engine.dispose()


class TestExtensionMissingSoftSkip:
    """When pg_stat_statements isn't loaded, the pipeline soft-skips.
    Use the shared `_pg_container` fixture from `conftest.py` (which
    does NOT preload the extension) to exercise the soft-skip path
    against a real Postgres.
    """

    def test_unavailable_extension_returns_skipped_report(
        self, pg_url: str, tmp_path: Path
    ) -> None:
        engine = create_engine(pg_url)
        try:
            with SQLiteStore(tmp_path / "s.db") as store:
                _seed_indexed_tables(store, source_id="src")
                report = mine_queries(
                    engine=engine,
                    store=store,
                    source_connection_id="src",
                )
                assert report.skipped_unavailable is True
                assert report.statements_read == 0
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
        finally:
            engine.dispose()
