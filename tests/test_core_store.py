"""Tests for schemabrain.core.store."""

from pathlib import Path

import pytest

from schemabrain.core.models import Column, ForeignKey, Table
from schemabrain.core.store import SchemaVersionMismatchError, SQLiteStore


def _column(
    name: str,
    *,
    table_name: str = "users",
    schema_name: str = "public",
    data_type: str = "text",
    nullable: bool = True,
    ordinal_position: int = 1,
    default: str | None = None,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table_name,
        schema_name=schema_name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=ordinal_position,
        default=default,
        is_primary_key=is_primary_key,
    )


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            _column(
                "id", data_type="bigint", nullable=False, ordinal_position=1, is_primary_key=True
            ),
            _column("email", data_type="text", nullable=False, ordinal_position=2),
            _column(
                "created_at",
                data_type="timestamp with time zone",
                nullable=False,
                ordinal_position=3,
                default="now()",
            ),
        ),
    )


def _members_table() -> Table:
    return Table(
        name="org_members",
        schema_name="public",
        columns=(
            _column(
                "org_id",
                table_name="org_members",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "user_id",
                table_name="org_members",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
                is_primary_key=True,
            ),
            _column(
                "role",
                table_name="org_members",
                data_type="text",
                nullable=True,
                ordinal_position=3,
            ),
        ),
        foreign_keys=(
            ForeignKey(
                name="org_members_org_id_fkey",
                source_columns=("org_id",),
                target_schema="public",
                target_table="orgs",
                target_columns=("id",),
            ),
            ForeignKey(
                name="org_members_user_id_fkey",
                source_columns=("user_id",),
                target_schema="public",
                target_table="users",
                target_columns=("id",),
            ),
        ),
    )


SOURCE_A = "src_a"
SOURCE_B = "src_b"


class TestLifecycle:
    def test_creates_file_on_first_open(self, tmp_path: Path):
        db_path = tmp_path / "schemabrain.db"
        assert not db_path.exists()
        store = SQLiteStore(db_path)
        store.close()
        assert db_path.exists()

    def test_creates_parent_directories(self, tmp_path: Path):
        db_path = tmp_path / "nested" / "deeply" / "store.db"
        store = SQLiteStore(db_path)
        store.close()
        assert db_path.exists()

    def test_close_is_idempotent(self, tmp_path: Path):
        store = SQLiteStore(tmp_path / "store.db")
        store.close()
        store.close()  # second call must not raise

    def test_use_after_close_raises(self, tmp_path: Path):
        store = SQLiteStore(tmp_path / "store.db")
        store.close()
        with pytest.raises(RuntimeError, match="closed"):
            store.list_tables()

    def test_context_manager_closes_on_exit(self, tmp_path: Path):
        store = SQLiteStore(tmp_path / "store.db")
        with store:
            store.list_tables()
        with pytest.raises(RuntimeError, match="closed"):
            store.list_tables()

    def test_in_memory_store(self):
        with SQLiteStore(":memory:") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            assert store.list_tables() == [("public", "users")]


class TestRoundTrip:
    def test_get_returns_none_for_missing_table(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.get_table("public", "missing", source_connection_id=SOURCE_A) is None

    def test_writes_and_reads_simple_table(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            got = store.get_table("public", "users", source_connection_id=SOURCE_A)
        assert got == _users_table()

    def test_writes_and_reads_table_with_foreign_keys(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_members_table(), source_connection_id=SOURCE_A)
            got = store.get_table("public", "org_members", source_connection_id=SOURCE_A)
        assert got == _members_table()

    def test_preserves_column_default_with_special_characters(self, tmp_path: Path):
        col = _column(
            "id",
            table_name="t",
            data_type="bigint",
            nullable=False,
            ordinal_position=1,
            default="nextval('t_id_seq'::regclass)",
            is_primary_key=True,
        )
        table = Table(name="t", schema_name="public", columns=(col,))
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(table, source_connection_id=SOURCE_A)
            got = store.get_table("public", "t", source_connection_id=SOURCE_A)
        assert got is not None
        assert got.get_column("id").default == "nextval('t_id_seq'::regclass)"

    def test_round_trip_with_composite_foreign_key(self, tmp_path: Path):
        cols = (
            _column(
                "tenant_id",
                table_name="memberships",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
            _column(
                "user_id",
                table_name="memberships",
                data_type="bigint",
                nullable=False,
                ordinal_position=2,
                is_primary_key=True,
            ),
        )
        fk = ForeignKey(
            name="memberships_composite_fkey",
            source_columns=("tenant_id", "user_id"),
            target_schema="public",
            target_table="tenant_users",
            target_columns=("tenant_id", "id"),
        )
        table = Table(name="memberships", schema_name="public", columns=cols, foreign_keys=(fk,))
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(table, source_connection_id=SOURCE_A)
            got = store.get_table("public", "memberships", source_connection_id=SOURCE_A)
        assert got == table


class TestUpsert:
    def test_writing_same_table_twice_replaces_old_rows(self, tmp_path: Path):
        original = _users_table()
        # Same name/schema, but with one fewer column
        modified = Table(
            name="users",
            schema_name="public",
            columns=(
                _column(
                    "id",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
            ),
        )
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(original, source_connection_id=SOURCE_A)
            store.write_table(modified, source_connection_id=SOURCE_A)
            got = store.get_table("public", "users", source_connection_id=SOURCE_A)
        assert got == modified
        assert len(got.columns) == 1

    def test_writing_same_table_to_different_sources_keeps_both(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_users_table(), source_connection_id=SOURCE_B)
            from_a = store.get_table("public", "users", source_connection_id=SOURCE_A)
            from_b = store.get_table("public", "users", source_connection_id=SOURCE_B)
        assert from_a is not None
        assert from_b is not None
        assert from_a == from_b


class TestListTables:
    def test_empty_store_returns_empty_list(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            assert store.list_tables() == []

    def test_lists_all_written_tables(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_members_table(), source_connection_id=SOURCE_A)
            assert sorted(store.list_tables()) == [
                ("public", "org_members"),
                ("public", "users"),
            ]

    def test_filters_by_source_connection_id(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_members_table(), source_connection_id=SOURCE_B)
            assert store.list_tables(source_connection_id=SOURCE_A) == [("public", "users")]
            assert store.list_tables(source_connection_id=SOURCE_B) == [("public", "org_members")]

    def test_lists_across_sources_when_no_filter(self, tmp_path: Path):
        with SQLiteStore(tmp_path / "store.db") as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
            store.write_table(_members_table(), source_connection_id=SOURCE_B)
            tables = store.list_tables()
        assert sorted(tables) == [("public", "org_members"), ("public", "users")]


class TestPersistence:
    def test_data_survives_reopening_the_store(self, tmp_path: Path):
        db_path = tmp_path / "store.db"
        with SQLiteStore(db_path) as store:
            store.write_table(_users_table(), source_connection_id=SOURCE_A)
        with SQLiteStore(db_path) as store:
            got = store.get_table("public", "users", source_connection_id=SOURCE_A)
        assert got == _users_table()


class TestSchemaVersion:
    def test_raises_on_incompatible_stored_version(self, tmp_path: Path):
        import sqlite3

        db_path = tmp_path / "store.db"
        SQLiteStore(db_path).close()
        # Simulate a future binary that wrote a newer schema version
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE schemabrain_meta SET value = '999' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        with pytest.raises(SchemaVersionMismatchError, match="999"):
            SQLiteStore(db_path)


SOURCE_X = "src_x"
SOURCE_Y = "src_y"


class TestDeleteTable:
    def test_removes_table_columns_and_fks(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        assert store.list_tables(source_connection_id=SOURCE_X) == [("public", "users")]
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        assert store.list_tables(source_connection_id=SOURCE_X) == []
        assert store.get_table("public", "users", source_connection_id=SOURCE_X) is None
        store.close()

    def test_idempotent_on_missing_table(self) -> None:
        store = SQLiteStore(":memory:")
        # No write — table doesn't exist. Delete must be a silent no-op.
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        store.close()

    def test_isolates_by_source_connection_id(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table(_users_table(), source_connection_id=SOURCE_Y)
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        # SOURCE_X gone; SOURCE_Y intact.
        assert store.list_tables(source_connection_id=SOURCE_X) == []
        assert store.list_tables(source_connection_id=SOURCE_Y) == [("public", "users")]
        store.close()


class TestColumnFingerprints:
    def test_get_returns_empty_dict_when_none_stored(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_write_then_get_round_trip(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        fps = {
            "id": ("struct_id", "sem_id"),
            "email": ("struct_email", "sem_email"),
            "created_at": ("struct_ts", "sem_ts"),
        }
        store.write_table_fingerprints(
            "public", "users", source_connection_id=SOURCE_X, fingerprints=fps
        )
        got = store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X)
        assert got == fps
        store.close()

    def test_write_replaces_existing(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            fingerprints={"id": ("v1_struct", "v1_sem")},
        )
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            fingerprints={"id": ("v2_struct", "v2_sem")},
        )
        got = store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X)
        assert got == {"id": ("v2_struct", "v2_sem")}
        store.close()

    def test_write_isolates_by_source_connection_id(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table(_users_table(), source_connection_id=SOURCE_Y)
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            fingerprints={"id": ("x_struct", "x_sem")},
        )
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_Y,
            fingerprints={"id": ("y_struct", "y_sem")},
        )
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X) == {
            "id": ("x_struct", "x_sem")
        }
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_Y) == {
            "id": ("y_struct", "y_sem")
        }
        store.close()

    def test_delete_table_cascades_to_fingerprints(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            fingerprints={"id": ("s", "m")},
        )
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        # Cascade — fingerprints gone too.
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_write_table_cascades_clears_old_fingerprints(self) -> None:
        # write_table does DELETE+INSERT on the parent row, which cascades
        # to delete column_fingerprints. Confirm this so the indexer flow
        # (write_table then write_table_fingerprints) is well-defined.
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            fingerprints={"id": ("old_s", "old_m")},
        )
        # Re-write the same table: old fingerprints get cleared.
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_write_fingerprints_without_table_row_raises(self) -> None:
        # FK enforces that the parent table row exists. Document this.
        import sqlite3

        store = SQLiteStore(":memory:")
        with pytest.raises(sqlite3.IntegrityError):
            store.write_table_fingerprints(
                "public",
                "missing",
                source_connection_id=SOURCE_X,
                fingerprints={"col": ("s", "m")},
            )
        store.close()

    def test_empty_fingerprints_dict_is_a_clear(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_fingerprints(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            fingerprints={"id": ("s", "m")},
        )
        store.write_table_fingerprints(
            "public", "users", source_connection_id=SOURCE_X, fingerprints={}
        )
        assert store.get_table_fingerprints("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()
