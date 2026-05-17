"""Indexer wiring tests for the PII classifier.

Asserts that:
  - The default path populates `column_pii_tags` from heuristic output.
  - `no_pii_classify=True` wipes existing tags for re-profiled tables.
  - Dropped tables don't leave orphan tag rows.
  - Unchanged tables retain their tags across a re-index.
"""

from __future__ import annotations

from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.indexer import index

SOURCE_ID = "pii_test_source"


def _col(
    name: str,
    *,
    table_name: str,
    ordinal: int,
    data_type: str = "TEXT",
    nullable: bool = True,
    is_pk: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name="public",
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_pk,
    )


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            _col(
                "id",
                table_name="users",
                ordinal=1,
                data_type="BIGINT",
                nullable=False,
                is_pk=True,
            ),
            _col("email", table_name="users", ordinal=2, nullable=False),
            _col("phone", table_name="users", ordinal=3),
            _col("signup_at", table_name="users", ordinal=4, data_type="TIMESTAMP"),
        ),
    )


def _orders_table() -> Table:
    return Table(
        name="orders",
        schema_name="public",
        columns=(
            _col(
                "id",
                table_name="orders",
                ordinal=1,
                data_type="BIGINT",
                nullable=False,
                is_pk=True,
            ),
            _col("amount", table_name="orders", ordinal=2),
            _col("created_at", table_name="orders", ordinal=3, data_type="TIMESTAMP"),
        ),
    )


def _stats_for(table: Table) -> dict:
    from schemabrain.profiler.stats import ColumnStats

    return {
        col.name: ColumnStats(
            column_name=col.name,
            total_rows=10,
            null_count=0,
            distinct_count=10,
            sample_values=("a", "b"),
        )
        for col in table.columns
    }


class _FakeDataSource:
    def __init__(self, tables: list[Table]) -> None:
        self._tables = {(t.schema_name, t.name): t for t in tables}

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        return list(self._tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        return self._tables[(schema, name)]

    def close(self) -> None:
        pass


class _CountingProfiler:
    def __init__(self) -> None:
        self.profile_calls: list[Table] = []

    def profile_table(self, table: Table) -> dict:
        self.profile_calls.append(table)
        return _stats_for(table)


class TestDefaultClassificationPath:
    def test_first_index_populates_tags_for_known_pii_columns(self) -> None:
        source = _FakeDataSource([_users_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            tags = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.users",
                columns=["id", "email", "phone", "signup_at"],
            )
            # Heuristic-positive rows only — `id` and `signup_at` are
            # stored as ("public", frozenset()) per Decision 9, so they
            # land in the result dict; assert them explicitly.
            assert tags["email"] == ("pii", frozenset({"contact"}))
            assert tags["phone"] == ("pii", frozenset({"contact"}))
            assert tags["id"] == ("public", frozenset())
            assert tags["signup_at"] == ("public", frozenset())
        finally:
            store.close()

    def test_amount_column_tagged_financial(self) -> None:
        source = _FakeDataSource([_orders_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            tags = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.orders",
                columns=["amount"],
            )
            assert tags["amount"] == ("pii", frozenset({"financial"}))
        finally:
            store.close()


class TestNoPiiClassifyOptOut:
    def test_no_pii_classify_writes_no_tags(self) -> None:
        source = _FakeDataSource([_users_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                no_pii_classify=True,
            )
            tags = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.users",
                columns=["email", "phone"],
            )
            assert tags == {}
        finally:
            store.close()

    def test_no_pii_classify_wipes_existing_tags(self) -> None:
        # First index with classification — tags populated. Second
        # index with --no-pii-classify on the SAME schema reaches the
        # "table unchanged" branch (no diff), so tags survive. Force
        # a re-profile by mutating the schema between runs.
        source = _FakeDataSource([_users_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            # Add a column to force re-profile on the second run.
            users_v2 = Table(
                name="users",
                schema_name="public",
                columns=(
                    *_users_table().columns,
                    _col("salary", table_name="users", ordinal=5),
                ),
            )
            source2 = _FakeDataSource([users_v2])
            index(
                source=source2,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                no_pii_classify=True,
            )
            tags = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.users",
                columns=["email", "phone", "salary"],
            )
            # All tags wiped — even the previously-classified email/phone.
            assert tags == {}
        finally:
            store.close()


class TestDroppedTableCleanup:
    def test_removed_table_loses_its_tags(self) -> None:
        # First run indexes two tables and tags both. Second run drops
        # `orders` from source; the tag rows for `orders` must not
        # survive (no FK CASCADE from `tables` to `column_pii_tags`).
        source_v1 = _FakeDataSource([_users_table(), _orders_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source_v1,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            assert (
                store.get_column_pii_tags(
                    source_connection_id=SOURCE_ID,
                    qualified_table="public.orders",
                    columns=["amount"],
                )
                != {}
            ), "precondition: orders tags must exist before drop"
            source_v2 = _FakeDataSource([_users_table()])
            index(
                source=source_v2,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            orphans = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.orders",
                columns=["amount"],
            )
            assert orphans == {}, "orphan PII tags left behind after table drop"
        finally:
            store.close()


class TestNoPiiClassifyPartialStateWarning:
    def test_warning_fires_when_unchanged_tables_retain_tags(
        self,
        capsys,
    ) -> None:
        # First run with classification — tags populated on both tables.
        source = _FakeDataSource([_users_table(), _orders_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            capsys.readouterr()  # discard first-run stderr
            # Second run with --no-pii-classify on the SAME schema —
            # both tables hit the "unchanged" branch. The classifier
            # never runs. The warning must surface so operators see
            # the partial state.
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                no_pii_classify=True,
            )
            stderr = capsys.readouterr().err
            assert "--no-pii-classify wiped tags for re-profiled tables" in stderr
            assert "2 unchanged table(s) retain" in stderr
        finally:
            store.close()

    def test_no_warning_when_all_tables_re_profiled(
        self,
        capsys,
    ) -> None:
        # Fresh store + --no-pii-classify on first index → every table
        # is "changed" (col_added for every column), zero unchanged.
        # Warning must NOT fire.
        source = _FakeDataSource([_users_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
                no_pii_classify=True,
            )
            stderr = capsys.readouterr().err
            assert "--no-pii-classify wiped tags" not in stderr
        finally:
            store.close()


class TestUnchangedTableTagsSurvive:
    def test_reindex_unchanged_schema_preserves_tags(self) -> None:
        source = _FakeDataSource([_users_table()])
        profiler = _CountingProfiler()
        store = SQLiteStore(":memory:")
        try:
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            tags_after_first = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.users",
                columns=["email"],
            )
            # Re-index with no schema changes — unchanged-table branch
            # taken; the classifier MUST NOT re-run.
            index(
                source=source,
                profiler=profiler,
                store=store,
                source_connection_id=SOURCE_ID,
            )
            tags_after_second = store.get_column_pii_tags(
                source_connection_id=SOURCE_ID,
                qualified_table="public.users",
                columns=["email"],
            )
            assert tags_after_first == tags_after_second
            assert tags_after_first["email"] == ("pii", frozenset({"contact"}))
        finally:
            store.close()
