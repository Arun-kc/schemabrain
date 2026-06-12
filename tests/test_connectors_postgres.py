"""Integration tests for PostgresDataSource against a real Postgres."""

import pytest

from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.connectors.postgres import PostgresDataSource

pytestmark = pytest.mark.integration


class TestListTables:
    def test_returns_seeded_tables_across_schemas(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = sorted(ds.list_tables())
        assert ("public", "orgs") in tables
        assert ("public", "users") in tables
        assert ("public", "org_members") in tables
        assert ("audit", "events") in tables

    def test_skips_system_schemas(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = list(ds.list_tables())
        for schema, _ in tables:
            assert not schema.startswith("pg_")
            assert schema != "information_schema"

    def test_filters_by_schema(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = sorted(ds.list_tables(schema="audit"))
        assert tables == [("audit", "events")]

    def test_filter_returns_empty_for_unknown_schema(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = list(ds.list_tables(schema="does_not_exist"))
        assert tables == []


class TestPartitionFiltering:
    def test_excludes_partition_children_from_schema_listing(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = sorted(ds.list_tables(schema="partitioning"))
        assert tables == [
            ("partitioning", "events_by_region"),
            ("partitioning", "orders_by_region"),
        ]

    def test_excludes_partition_children_from_full_listing(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = ds.list_tables()
        assert ("partitioning", "events_by_region") in tables
        assert ("partitioning", "events_us") not in tables
        assert ("partitioning", "events_eu") not in tables
        assert ("partitioning", "orders_by_region") in tables
        assert ("partitioning", "orders_by_region_us") not in tables
        assert ("partitioning", "orders_by_region_eu") not in tables

    def test_get_table_still_returns_partition_child_when_explicitly_requested(
        self, seeded_pg_url: str
    ):
        # list_tables() filters them, but get_table() stays permissive
        # so callers can still inspect a child directly if they want.
        with PostgresDataSource(seeded_pg_url) as ds:
            child = ds.get_table("events_us", schema="partitioning")
        assert child.name == "events_us"
        assert child.schema_name == "partitioning"

    def test_get_table_returns_parent_with_inherited_columns(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            parent = ds.get_table("events_by_region", schema="partitioning")
        col_names = {c.name for c in parent.columns}
        assert col_names == {"id", "region", "payload"}
        assert sorted(parent.primary_key_columns()) == ["id", "region"]

    def test_get_table_unions_fks_from_partition_children(self, seeded_pg_url: str):
        # Pagila pattern: the FK on `user_id -> users.id` is declared on
        # `orders_by_region_us` (a partition child), NOT on the parent.
        # Postgres does NOT propagate child-only FKs up to the parent, so
        # `inspector.get_foreign_keys('orders_by_region')` returns []. The
        # connector must peek at a representative child and surface the
        # child's FK as if it lived on the parent — otherwise the
        # downstream entity-detection / join-inference paths see the
        # partition parent as a relationship-less island.
        with PostgresDataSource(seeded_pg_url) as ds:
            parent = ds.get_table("orders_by_region", schema="partitioning")
        fk_targets = sorted((fk.target_table, fk.source_columns[0]) for fk in parent.foreign_keys)
        assert fk_targets == [("users", "user_id")]
        # Target schema resolves to `public` (the FK's referred_schema),
        # not the parent's `partitioning` schema.
        assert parent.foreign_keys[0].target_schema == "public"
        assert parent.foreign_keys[0].target_columns == ("id",)

    def test_get_table_dedups_when_fk_lives_on_both_parent_and_children(self, seeded_pg_url: str):
        # The `events_by_region` parent has no FKs declared anywhere
        # (parent has no FKs AND its children have no FKs). The connector
        # must NOT invent FKs out of thin air on partition parents
        # whose children are also FK-less.
        with PostgresDataSource(seeded_pg_url) as ds:
            parent = ds.get_table("events_by_region", schema="partitioning")
        assert parent.foreign_keys == ()


class TestGetTable:
    def test_returns_table_with_expected_columns(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            users = ds.get_table("users", schema="public")
        assert users.name == "users"
        assert users.schema_name == "public"
        col_names = {c.name for c in users.columns}
        assert col_names == {"id", "email", "created_at"}

    def test_marks_single_primary_key_column(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            users = ds.get_table("users", schema="public")
        assert users.primary_key_columns() == ("id",)

    def test_detects_composite_primary_key(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            members = ds.get_table("org_members", schema="public")
        assert sorted(members.primary_key_columns()) == ["org_id", "user_id"]

    def test_captures_foreign_keys(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            members = ds.get_table("org_members", schema="public")
        fk_targets = sorted((fk.target_table, fk.source_columns[0]) for fk in members.foreign_keys)
        assert fk_targets == [("orgs", "org_id"), ("users", "user_id")]

    def test_foreign_key_target_columns_match_referenced_pk(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            members = ds.get_table("org_members", schema="public")
        for fk in members.foreign_keys:
            assert fk.target_columns == ("id",)
            assert fk.target_schema == "public"

    def test_captures_default_value_for_created_at(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            users = ds.get_table("users", schema="public")
        created_at = users.get_column("created_at")
        assert created_at is not None
        assert created_at.default is not None
        assert "now" in created_at.default.lower()

    def test_captures_nullable_flag(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            users = ds.get_table("users", schema="public")
            members = ds.get_table("org_members", schema="public")
        email = users.get_column("email")
        assert email is not None
        assert email.nullable is False
        role = members.get_column("role")
        assert role is not None
        assert role.nullable is True

    def test_columns_are_ordinally_positioned(self, seeded_pg_url: str):
        with PostgresDataSource(seeded_pg_url) as ds:
            users = ds.get_table("users", schema="public")
        positions = sorted(c.ordinal_position for c in users.columns)
        assert positions == list(range(1, len(positions) + 1))

    def test_raises_table_not_found_on_unknown_table(self, seeded_pg_url: str):
        with (
            PostgresDataSource(seeded_pg_url) as ds,
            pytest.raises(TableNotFoundError, match="not found"),
        ):
            ds.get_table("does_not_exist", schema="public")

    def test_table_not_found_is_a_value_error(self, seeded_pg_url: str):
        with (
            PostgresDataSource(seeded_pg_url) as ds,
            pytest.raises(ValueError),
        ):
            ds.get_table("does_not_exist", schema="public")


class TestLifecycle:
    def test_close_is_idempotent(self, seeded_pg_url: str):
        ds = PostgresDataSource(seeded_pg_url)
        ds.close()
        ds.close()  # second call must not raise

    def test_use_after_close_raises(self, seeded_pg_url: str):
        ds = PostgresDataSource(seeded_pg_url)
        ds.close()
        with pytest.raises(RuntimeError, match="closed"):
            list(ds.list_tables())

    def test_context_manager_closes_on_exit(self, seeded_pg_url: str):
        ds = PostgresDataSource(seeded_pg_url)
        with ds:
            list(ds.list_tables())
        with pytest.raises(RuntimeError, match="closed"):
            list(ds.list_tables())


class TestSourceConnectionIsReadOnly:
    """The source database connection MUST be read-only at the Postgres
    session level.

    SECURITY.md ("Security Posture Today") promises that the profiler
    issues `SELECT` queries only. Code review enforced that promise
    until 2026-05-15. From that date, `default_transaction_read_only=on`
    enforces it at the wire too — any future regression that tries to
    INSERT/UPDATE/DELETE/DROP fails with a clear Postgres error rather
    than silently mutating a customer database.

    These tests verify the server-side enforcement is actually
    configured (not just the connect_args dict; the GUC takes effect
    on the live session).
    """

    def test_insert_against_seeded_table_raises_read_only(self, seeded_pg_url: str) -> None:
        # An INSERT through the DataSource's engine must be rejected by
        # Postgres itself (error code 25006: read_only_sql_transaction).
        # The exact error class comes from psycopg's mapping; we match
        # on the SQLSTATE-derived message text which is stable.
        from sqlalchemy import text
        from sqlalchemy.exc import InternalError

        with PostgresDataSource(seeded_pg_url) as ds:
            engine = ds._require_engine()
            with engine.connect() as conn, pytest.raises(InternalError, match="read-only"):
                conn.execute(text("INSERT INTO public.users (id) VALUES (-1)"))

    def test_create_table_against_engine_raises_read_only(self, seeded_pg_url: str) -> None:
        # DDL (CREATE TABLE) is also blocked under
        # default_transaction_read_only=on. Belt-and-suspenders coverage
        # so a future regression that tries to materialize a temp table
        # for query optimization (a real anti-pattern in profilers)
        # fails fast.
        from sqlalchemy import text
        from sqlalchemy.exc import InternalError

        with PostgresDataSource(seeded_pg_url) as ds:
            engine = ds._require_engine()
            with engine.connect() as conn, pytest.raises(InternalError, match="read-only"):
                conn.execute(text("CREATE TABLE public.__sb_test_ro_marker (id INT)"))

    def test_select_still_works(self, seeded_pg_url: str) -> None:
        # Sanity: the read-only flag must NOT break SELECT. Without
        # this test, an accidentally-too-strict GUC could pass the two
        # rejection tests above while silently breaking the actual
        # profiler. `list_tables` issues SELECTs against `pg_catalog`,
        # which is the same query shape the profiler uses.
        with PostgresDataSource(seeded_pg_url) as ds:
            tables = list(ds.list_tables())
            assert len(tables) > 0


class TestEstimatedRowCount:
    """`estimated_row_count` reads `pg_class.reltuples`.

    Uses a throwaway schema created through a separate writable engine
    (the connector enforces read-only on its own connection) so the
    assertions are deterministic and don't depend on autovacuum timing
    against the shared seeded tables. The schema is dropped in `finally`.
    """

    def test_returns_analyzed_estimate(self, pg_url: str) -> None:
        from sqlalchemy import create_engine, text

        engine = create_engine(pg_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE SCHEMA rowcount_probe"))
                conn.execute(text("CREATE TABLE rowcount_probe.probe (id BIGINT)"))
                conn.execute(
                    text(
                        "INSERT INTO rowcount_probe.probe "
                        "SELECT g FROM generate_series(1, 500) AS g"
                    )
                )
                # ANALYZE on a 500-row table samples every row, so
                # reltuples lands exactly on 500 — no estimate fuzz.
                conn.execute(text("ANALYZE rowcount_probe.probe"))
            with PostgresDataSource(pg_url) as ds:
                assert ds.estimated_row_count("probe", "rowcount_probe") == 500
        finally:
            with engine.begin() as conn:
                conn.execute(text("DROP SCHEMA IF EXISTS rowcount_probe CASCADE"))
            engine.dispose()

    def test_returns_none_for_never_analyzed_table(self, pg_url: str) -> None:
        # A freshly created, never-ANALYZE'd table has reltuples = -1 on
        # PG14+ (the "unknown" sentinel) — must surface as None, not a
        # fabricated 0.
        from sqlalchemy import create_engine, text

        engine = create_engine(pg_url)
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE SCHEMA rowcount_unanalyzed"))
                conn.execute(text("CREATE TABLE rowcount_unanalyzed.fresh (id BIGINT)"))
            with PostgresDataSource(pg_url) as ds:
                assert ds.estimated_row_count("fresh", "rowcount_unanalyzed") is None
        finally:
            with engine.begin() as conn:
                conn.execute(text("DROP SCHEMA IF EXISTS rowcount_unanalyzed CASCADE"))
            engine.dispose()

    def test_returns_none_for_missing_table(self, seeded_pg_url: str) -> None:
        with PostgresDataSource(seeded_pg_url) as ds:
            assert ds.estimated_row_count("does_not_exist", "public") is None
