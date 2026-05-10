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
