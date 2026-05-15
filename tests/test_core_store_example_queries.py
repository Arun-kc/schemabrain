"""Tests for the v0.5 `example_queries` storage layer.

The example_queries table is the storage primitive behind tool #5
`get_example_queries`. v0.5 ships the read side; the write side
(`pg_stat_statements` mining + `sqlglot` parsing) lands in the next
PR. Tests inject rows via direct SQL — the test scaffolding is the
substitute for the not-yet-written mining writer.

The ExampleQuery dataclass carries `sensitivity` and `pii_categories`
fields from day one per Phase B ADR §3 — they default to public /
empty until mining computes real propagated values.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.example_query import ExampleQuery
from schemabrain.core.store import SQLiteStore
from tests._example_query_helpers import (
    insert_example_query as _insert_example,
)
from tests._example_query_helpers import (
    seed_table_for_examples as _seed_table,
)


class TestExampleQueriesTablePresent:
    """The `example_queries` table is the storage primitive these
    tests exercise — pin its presence so a future DDL refactor that
    accidentally drops it fails CI here, where the failure name is
    informative.
    """

    def test_fresh_store_has_example_queries_table(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "sb.db") as store:
            row = (
                store._require_conn()
                .execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'example_queries'"
                )
                .fetchone()
            )
            assert row is not None
            assert row["name"] == "example_queries"


class TestExampleQueryDataclass:
    """`ExampleQuery` is a frozen dataclass. PII fields default to
    empty / public per the Phase B ADR's day-one shape.
    """

    def test_default_pii_fields(self) -> None:
        eq = ExampleQuery(
            schema_name="public",
            table_name="orders",
            sql_text="SELECT * FROM public.orders LIMIT 10",
            observation_count=1,
            first_seen_at=0,
            last_seen_at=0,
            source="pg_stat_statements",
        )
        assert eq.sensitivity == "public"
        assert eq.pii_categories == frozenset()

    def test_frozen(self) -> None:
        eq = ExampleQuery(
            schema_name="public",
            table_name="orders",
            sql_text="SELECT 1",
            observation_count=1,
            first_seen_at=0,
            last_seen_at=0,
            source="curated",
        )
        with pytest.raises(Exception, match=r"frozen|cannot assign"):
            eq.observation_count = 2  # type: ignore[misc]

    def test_pii_fields_carry_through(self) -> None:
        eq = ExampleQuery(
            schema_name="public",
            table_name="users",
            sql_text="SELECT email FROM users",
            observation_count=42,
            first_seen_at=0,
            last_seen_at=0,
            source="pg_stat_statements",
            sensitivity="pii",
            pii_categories=frozenset({"contact"}),
        )
        assert eq.sensitivity == "pii"
        assert eq.pii_categories == frozenset({"contact"})

    def test_pii_sensitivity_without_categories_raises(self) -> None:
        # Phase B ADR §3 cross-layer invariant: `sensitivity="pii"`
        # must carry at least one category. Locked from day one so
        # the mining writer can't ship rows that violate it.
        with pytest.raises(ValueError, match="at least one pii_category"):
            ExampleQuery(
                schema_name="public",
                table_name="users",
                sql_text="SELECT 1",
                observation_count=1,
                first_seen_at=0,
                last_seen_at=0,
                source="pg_stat_statements",
                sensitivity="pii",
                pii_categories=frozenset(),
            )

    def test_non_pii_sensitivity_with_categories_raises(self) -> None:
        # The mirror half of the invariant: `sensitivity != "pii"`
        # cannot carry categories.
        with pytest.raises(ValueError, match="cannot carry pii_categories"):
            ExampleQuery(
                schema_name="public",
                table_name="users",
                sql_text="SELECT 1",
                observation_count=1,
                first_seen_at=0,
                last_seen_at=0,
                source="pg_stat_statements",
                sensitivity="confidential",
                pii_categories=frozenset({"contact"}),
            )


class TestListExampleQueriesEmpty:
    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            assert (
                store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=10,
                )
                == []
            )

    def test_unknown_table_returns_empty_list(self, tmp_path: Path) -> None:
        # No row for ('public','nope') → empty list, NOT an error. The
        # store layer is a dumb projection; "did this table exist?" is
        # the MCP layer's call against the `tables` projection.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            assert (
                store.list_example_queries(
                    schema="public",
                    table="nope",
                    source_connection_id="src",
                    limit=10,
                )
                == []
            )


class TestListExampleQueriesBasic:
    def test_single_row_round_trips(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="SELECT id, total FROM public.orders WHERE user_id = $1",
                observation_count=12,
                first_seen_at=1_700_000_000,
                last_seen_at=1_700_009_000,
                source="pg_stat_statements",
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(rows) == 1
            r = rows[0]
            assert r.schema_name == "public"
            assert r.table_name == "orders"
            assert r.sql_text == "SELECT id, total FROM public.orders WHERE user_id = $1"
            assert r.observation_count == 12
            assert r.first_seen_at == 1_700_000_000
            assert r.last_seen_at == 1_700_009_000
            assert r.source == "pg_stat_statements"
            assert r.sensitivity == "public"
            assert r.pii_categories == frozenset()

    def test_pii_categories_string_round_trips_to_frozenset(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="users")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="users",
                sql_text="SELECT email, phone FROM users",
                sensitivity="pii",
                pii_categories="contact,online_identifier",
            )
            rows = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert len(rows) == 1
            assert rows[0].sensitivity == "pii"
            assert rows[0].pii_categories == frozenset({"contact", "online_identifier"})

    def test_empty_pii_categories_string_is_empty_frozenset(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="SELECT 1 FROM orders",
                pii_categories="",  # explicit empty
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert rows[0].pii_categories == frozenset()


class TestListExampleQueriesOrdering:
    """Ranking contract: observation_count DESC, last_seen_at DESC,
    id ASC. Most-observed first; recency tiebreak; insertion order
    settles any remaining tie deterministically.
    """

    def test_orders_by_observation_count_desc(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="rare",
                observation_count=1,
                last_seen_at=1_700_000_000,
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="popular",
                observation_count=99,
                last_seen_at=1_700_000_000,
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="medium",
                observation_count=10,
                last_seen_at=1_700_000_000,
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert [r.sql_text for r in rows] == ["popular", "medium", "rare"]

    def test_tied_observation_count_orders_by_last_seen_desc(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="older",
                observation_count=5,
                last_seen_at=1_700_000_000,
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="newer",
                observation_count=5,
                last_seen_at=1_700_009_000,
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert [r.sql_text for r in rows] == ["newer", "older"]

    def test_full_tie_orders_by_id_asc(self, tmp_path: Path) -> None:
        # Same observation_count, same last_seen_at — id ascending is
        # the deterministic last tiebreak, so the FIRST inserted row
        # ranks first.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="first_inserted",
                observation_count=5,
                last_seen_at=1_700_000_000,
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="second_inserted",
                observation_count=5,
                last_seen_at=1_700_000_000,
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert [r.sql_text for r in rows] == ["first_inserted", "second_inserted"]


class TestListExampleQueriesIsolation:
    def test_source_isolation(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src_a", schema="public", table="orders")
            _seed_table(store, source_id="src_b", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src_a",
                schema="public",
                table="orders",
                sql_text="from_a",
            )
            _insert_example(
                store,
                source_id="src_b",
                schema="public",
                table="orders",
                sql_text="from_b",
            )
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
            assert {r.sql_text for r in rows_a} == {"from_a"}
            assert {r.sql_text for r in rows_b} == {"from_b"}

    def test_schema_isolation(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="staging", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="public_row",
            )
            _insert_example(
                store,
                source_id="src",
                schema="staging",
                table="orders",
                sql_text="staging_row",
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert {r.sql_text for r in rows} == {"public_row"}

    def test_table_isolation(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _seed_table(store, source_id="src", schema="public", table="users")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="orders_row",
            )
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="users",
                sql_text="users_row",
            )
            rows = store.list_example_queries(
                schema="public",
                table="users",
                source_connection_id="src",
                limit=10,
            )
            assert {r.sql_text for r in rows} == {"users_row"}


class TestListExampleQueriesLimit:
    def test_limit_caps_returned_rows(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            for i in range(15):
                _insert_example(
                    store,
                    source_id="src",
                    schema="public",
                    table="orders",
                    sql_text=f"sql_{i}",
                    observation_count=15 - i,  # decreasing so order is stable
                )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=5,
            )
            assert len(rows) == 5
            assert [r.sql_text for r in rows] == [f"sql_{i}" for i in range(5)]

    def test_limit_less_than_one_raises(self, tmp_path: Path) -> None:
        # Mirror the `search_embeddings_topk` contract: `k < 1` raises.
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            with pytest.raises(ValueError, match="limit"):
                store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=0,
                )

    def test_negative_limit_raises(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            with pytest.raises(ValueError, match="limit"):
                store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=-3,
                )


class TestListExampleQueriesSensitivityRoundTrip:
    """Every sensitivity value listed in the ADR round-trips. Catches
    accidental CHECK-constraint or Python-side validator drift if
    either side ever narrows the enum.
    """

    @pytest.mark.parametrize(
        "sensitivity",
        ["public", "internal", "confidential", "pii"],
    )
    def test_each_sensitivity_round_trips(self, tmp_path: Path, sensitivity: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            # `pii` sensitivity requires at least one category to be
            # internally consistent with the eventual entity-blueprint
            # validator; supply one so this case also exercises a
            # populated `pii_categories` field.
            pii_cats = "contact" if sensitivity == "pii" else ""
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text=f"sql_{sensitivity}",
                sensitivity=sensitivity,
                pii_categories=pii_cats,
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert len(rows) == 1
            assert rows[0].sensitivity == sensitivity


class TestListExampleQueriesSourceEnumRoundTrip:
    @pytest.mark.parametrize("source", ["pg_stat_statements", "curated"])
    def test_each_source_round_trips(self, tmp_path: Path, source: str) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text=f"sql_{source}",
                source=source,
            )
            rows = store.list_example_queries(
                schema="public",
                table="orders",
                source_connection_id="src",
                limit=10,
            )
            assert rows[0].source == source


class TestListExampleQueriesCheckConstraints:
    """The CHECK constraints on `source` and `sensitivity` are the
    SQL-layer enforcement of the Phase B ADR enums. They guard
    against any direct-SQL writer (mining, future curation tooling)
    that drifts off the closed set.
    """

    def test_invalid_source_rejected_by_check_constraint(self, tmp_path: Path) -> None:
        import sqlite3

        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _insert_example(
                    store,
                    source_id="src",
                    schema="public",
                    table="orders",
                    sql_text="bad",
                    source="not_a_real_source",
                )

    def test_invalid_sensitivity_rejected_by_check_constraint(self, tmp_path: Path) -> None:
        import sqlite3

        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _insert_example(
                    store,
                    source_id="src",
                    schema="public",
                    table="orders",
                    sql_text="bad",
                    sensitivity="not_a_real_sensitivity",
                )


class TestListExampleQueriesCascadeOnTableDelete:
    """If the parent table is deleted, example_queries rows for that
    table go too — ON DELETE CASCADE.
    """

    def test_delete_table_cascades_to_example_queries(self, tmp_path: Path) -> None:
        with SQLiteStore(tmp_path / "s.db") as store:
            _seed_table(store, source_id="src", schema="public", table="orders")
            _insert_example(
                store,
                source_id="src",
                schema="public",
                table="orders",
                sql_text="sql_a",
            )
            assert (
                len(
                    store.list_example_queries(
                        schema="public",
                        table="orders",
                        source_connection_id="src",
                        limit=10,
                    )
                )
                == 1
            )
            store.delete_table(schema_name="public", name="orders", source_connection_id="src")
            assert (
                store.list_example_queries(
                    schema="public",
                    table="orders",
                    source_connection_id="src",
                    limit=10,
                )
                == []
            )
