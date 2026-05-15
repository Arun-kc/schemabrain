"""Tests for the v0.5 `example_queries` writer.

`SQLiteStore.write_example_queries` is the writer half of tool #5's
storage primitive (the read half landed with PR #20). The query log
mining pipeline (`schemabrain.mining`) is the only production caller;
the writer is exposed on `SQLiteStore` and the `Store` Protocol so a
future alternative miner (e.g. a hosted dashboard pipeline) can drop
in without subclassing.

The write semantics are UPSERT on `(source_connection_id, schema_name,
table_name, sql_text)`:
  - First write inserts; `first_seen_at` and `last_seen_at` both take
    the value passed by the caller.
  - Re-write of the same 4-tuple updates `observation_count`,
    `last_seen_at`, and `sensitivity`/`pii_categories` to whatever the
    caller now believes is true (pg_stat_statements is cumulative since
    last reset; the caller passes the absolute count).
  - `first_seen_at` is preserved across re-writes so a row's first
    observation timestamp survives re-mining.

A new UNIQUE INDEX `idx_example_queries_unique` enforces the conflict
target; schema version bumps 6 → 7 to land it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore
from tests._example_query_helpers import seed_table_for_examples as _seed_table


def _eq(
    *,
    schema: str = "public",
    table: str = "orders",
    sql_text: str = "SELECT id FROM orders",
    observation_count: int = 1,
    first_seen_at: int = 1_700_000_000,
    last_seen_at: int = 1_700_000_000,
    source: str = "pg_stat_statements",
    sensitivity: str = "public",
    pii_categories: frozenset[str] | None = None,
) -> ExampleQuery:
    """Compact constructor for test ExampleQuery rows."""
    return ExampleQuery(
        schema_name=schema,
        table_name=table,
        sql_text=sql_text,
        observation_count=observation_count,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        source=source,  # type: ignore[arg-type]
        sensitivity=sensitivity,  # type: ignore[arg-type]
        pii_categories=frozenset() if pii_categories is None else pii_categories,  # type: ignore[arg-type]
    )


class TestSchemaVersionBumpToV7:
    """Pre-alpha contract: a store written by a v6 Schema Brain raises
    `SchemaVersionMismatchError` on open. Per project convention only
    the current N-1 version-bump test is retained — the prior
    `TestSchemaVersionBumpToV6` class (added at the v5→v6 bump) was
    removed when this class was added.
    """

    def test_fresh_store_has_schema_version_7(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            row = (
                store._require_conn()
                .execute("SELECT value FROM schemabrain_meta WHERE key = 'schema_version'")
                .fetchone()
            )
            assert row["value"] == "7"

    def test_opening_a_v6_store_raises(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sb.db"
        store = SQLiteStore(db_path)
        store._require_conn().execute(
            "UPDATE schemabrain_meta SET value = '6' WHERE key = 'schema_version'"
        )
        store._require_conn().commit()
        store.close()
        with pytest.raises(SchemaVersionMismatchError, match=r"6.*7|7.*6"):
            SQLiteStore(db_path)

    def test_unique_index_exists(self, tmp_path: Path) -> None:
        # Without the UNIQUE index, the UPSERT's ON CONFLICT target is
        # invalid SQL — the index existence is load-bearing.
        with SQLiteStore(tmp_path / "sb.db") as store:
            row = (
                store._require_conn()
                .execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_example_queries_unique'"
                )
                .fetchone()
            )
            assert row is not None


class TestWriteExampleQueriesEmpty:
    def test_empty_list_writes_zero_rows(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            written = store.write_example_queries([], source_connection_id="src")
            assert written == 0
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert rows == []


class TestWriteExampleQueriesInsert:
    def test_single_insert_round_trips(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            row = _eq(
                sql_text="SELECT id, total FROM public.orders WHERE user_id = $1",
                observation_count=12,
                first_seen_at=1_700_000_000,
                last_seen_at=1_700_009_000,
            )
            written = store.write_example_queries([row], source_connection_id="src")
            assert written == 1
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].sql_text == row.sql_text
            assert stored[0].observation_count == 12
            assert stored[0].first_seen_at == 1_700_000_000
            assert stored[0].last_seen_at == 1_700_009_000

    def test_pii_fields_round_trip(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            row = _eq(
                schema="public",
                table="users",
                sql_text="SELECT email, phone FROM users",
                sensitivity="pii",
                pii_categories=frozenset({"contact", "online_identifier"}),
            )
            store.write_example_queries([row], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert stored[0].sensitivity == "pii"
            assert stored[0].pii_categories == frozenset({"contact", "online_identifier"})

    def test_empty_pii_categories_round_trips_to_empty_string(self, tmp_path: Path) -> None:
        # Storage shape: frozenset() → '' (not 'frozenset()' or anything
        # else literal). Round-trips cleanly via _decode_pii_categories.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            row = _eq(pii_categories=frozenset())
            store.write_example_queries([row], source_connection_id="src")
            raw = (
                store._require_conn()
                .execute("SELECT pii_categories FROM example_queries")
                .fetchone()
            )
            assert raw["pii_categories"] == ""

    def test_pii_categories_stored_in_sorted_order(self, tmp_path: Path) -> None:
        # Storage canonicalises the frozenset to a sorted CSV so two
        # callers writing the same conceptual set always produce the
        # same row — load-bearing for the UPSERT identity (the sql_text
        # is the unique-tuple element, but the PII storage must not
        # drift across re-writes that happen to compute the set in a
        # different iteration order).
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            row = _eq(
                schema="public",
                table="users",
                sensitivity="pii",
                pii_categories=frozenset({"online_identifier", "contact"}),
            )
            store.write_example_queries([row], source_connection_id="src")
            raw = (
                store._require_conn()
                .execute("SELECT pii_categories FROM example_queries")
                .fetchone()
            )
            assert raw["pii_categories"] == "contact,online_identifier"

    def test_batch_insert_returns_correct_count(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="public", table="users")
            rows = [
                _eq(sql_text="q1", table="orders"),
                _eq(sql_text="q2", table="orders"),
                _eq(sql_text="q3", table="users"),
            ]
            written = store.write_example_queries(rows, source_connection_id="src")
            assert written == 3


class TestWriteExampleQueriesUpsert:
    """Re-writing the same (source_id, schema, table, sql_text) tuple
    UPSERTs: count and last_seen_at update, first_seen_at is preserved.
    """

    def test_rewrite_uses_max_of_old_and_new_observation_count(self, tmp_path: Path) -> None:
        # pg_stat_statements.calls is cumulative since the last reset.
        # `pg_stat_statements_reset()` between mining runs would
        # otherwise let a smaller post-reset value overwrite a higher
        # historical count. MAX(...) preserves the higher value.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            first = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=100,
            )
            store.write_example_queries([first], source_connection_id="src")
            # Second mining: lower count (simulates a reset between runs).
            second = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=10,
            )
            store.write_example_queries([second], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            # The 100 must survive — the lower 10 doesn't clobber it.
            assert stored[0].observation_count == 100

    def test_rewrite_preserves_first_seen_at(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            first = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=10,
                first_seen_at=1_700_000_000,
                last_seen_at=1_700_000_500,
            )
            store.write_example_queries([first], source_connection_id="src")
            # Second mining run sees the same SQL with a higher count
            # and a later last_seen_at. The first_seen_at the second
            # call passes is IGNORED — the original sticks.
            second = _eq(
                sql_text="SELECT * FROM orders",
                observation_count=42,
                first_seen_at=1_700_100_000,  # different but ignored
                last_seen_at=1_700_100_500,
            )
            store.write_example_queries([second], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].observation_count == 42
            assert stored[0].first_seen_at == 1_700_000_000  # preserved
            assert stored[0].last_seen_at == 1_700_100_500  # updated

    def test_rewrite_updates_sensitivity_and_pii_categories(self, tmp_path: Path) -> None:
        # Mining initially classifies a query as `public`; after the
        # PII classifier ships, a re-mining of the same row reclassifies
        # it. The UPSERT must update those columns (not preserve them
        # like first_seen_at).
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            first = _eq(
                schema="public",
                table="users",
                sql_text="SELECT email FROM users",
                sensitivity="public",
                pii_categories=frozenset(),
            )
            store.write_example_queries([first], source_connection_id="src")
            second = _eq(
                schema="public",
                table="users",
                sql_text="SELECT email FROM users",
                sensitivity="pii",
                pii_categories=frozenset({"contact"}),
            )
            store.write_example_queries([second], source_connection_id="src")
            stored = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert len(stored) == 1
            assert stored[0].sensitivity == "pii"
            assert stored[0].pii_categories == frozenset({"contact"})


class TestWriteExampleQueriesIsolation:
    def test_source_isolation(self, tmp_path: Path) -> None:
        # Same sql_text under two sources is two distinct rows — the
        # UPSERT conflict target includes source_connection_id.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src_a", schema="public", table="orders")
            _seed_table(store, source_id="src_b", schema="public", table="orders")
            row = _eq(sql_text="SELECT 1 FROM orders")
            store.write_example_queries([row], source_connection_id="src_a")
            store.write_example_queries([row], source_connection_id="src_b")
            rows_a = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src_a",
                limit=10,
            )
            rows_b = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src_b",
                limit=10,
            )
            assert len(rows_a) == 1
            assert len(rows_b) == 1

    def test_same_sql_under_two_tables_writes_two_rows(self, tmp_path: Path) -> None:
        # Same SQL touching multiple tables (a JOIN) produces multiple
        # example_queries rows — one per touched table. The conflict
        # tuple includes table_name so this is two distinct rows.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="public", table="users")
            join_sql = "SELECT * FROM orders JOIN users ON users.id = orders.user_id"
            store.write_example_queries(
                [
                    _eq(table="orders", sql_text=join_sql),
                    _eq(table="users", sql_text=join_sql),
                ],
                source_connection_id="src",
            )
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
            assert len(users_rows) == 1


class TestWriteExampleQueriesForeignKeyContract:
    def test_write_for_unknown_table_raises_integrity_error(self, tmp_path: Path) -> None:
        # Caller is expected to filter to indexed tables before calling
        # the writer (the mining pipeline does). But if it doesn't, the
        # FK constraint catches it.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            row = _eq(table="nope")  # not seeded
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                store.write_example_queries([row], source_connection_id="src")


class TestWriteExampleQueriesAtomicity:
    """Within a single `write_example_queries` call, either every row
    lands or none do — the writer wraps the batch in a transaction.
    """

    def test_one_bad_row_rolls_back_the_batch(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            good = _eq(table="orders", sql_text="good")
            bad = _eq(table="nope", sql_text="bad")  # FK violation
            with pytest.raises(sqlite3.IntegrityError):
                store.write_example_queries([good, bad], source_connection_id="src")
            # The good row must NOT have been committed.
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert rows == []


class TestWriteExampleQueriesCheckConstraintsHonoured:
    """The SQL CHECK constraints on `source` and `sensitivity` continue
    to fire under the new writer — defense-in-depth alongside the
    Python-side Literal types.
    """

    def test_python_invariant_catches_bad_pii_combination_before_sql(self, tmp_path: Path) -> None:
        # `ExampleQuery.__post_init__` enforces the cross-layer
        # invariant; the writer never sees a violating row.
        with pytest.raises(ValueError, match="at least one pii_category"):
            _eq(sensitivity="pii", pii_categories=frozenset())
