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


class TestColumnDescriptions:
    def _desc(self, text: str = "User identifier") -> object:
        from schemabrain.core.description import ColumnDescription

        return ColumnDescription(
            text=text,
            model="claude-haiku-4-5",
            prompt_version="2026-05-10-1",
            input_tokens=300,
            cached_input_tokens=100,
            output_tokens=20,
            cost_usd=0.0003,
        )

    def test_get_returns_empty_dict_when_none_stored(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        assert store.get_table_descriptions("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_write_then_get_round_trip(self) -> None:
        from schemabrain.core.description import ColumnDescription

        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        descs = {
            "id": self._desc("Numeric primary key"),
            "email": self._desc("User's email address"),
        }
        store.write_table_descriptions(
            "public", "users", source_connection_id=SOURCE_X, descriptions=descs
        )
        got = store.get_table_descriptions("public", "users", source_connection_id=SOURCE_X)
        assert set(got.keys()) == {"id", "email"}
        assert got["id"].text == "Numeric primary key"
        assert got["email"].text == "User's email address"
        # Provenance fields round-trip too.
        assert got["id"].model == "claude-haiku-4-5"
        assert got["id"].prompt_version == "2026-05-10-1"
        assert got["id"].input_tokens == 300
        assert got["id"].cached_input_tokens == 100
        assert got["id"].output_tokens == 20
        assert got["id"].cost_usd == 0.0003
        assert isinstance(got["id"], ColumnDescription)
        store.close()

    def test_write_replaces_existing(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            descriptions={"id": self._desc("original")},
        )
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            descriptions={"id": self._desc("rewritten")},
        )
        got = store.get_table_descriptions("public", "users", source_connection_id=SOURCE_X)
        assert got["id"].text == "rewritten"
        store.close()

    def test_delete_table_cascades_to_descriptions(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            descriptions={"id": self._desc()},
        )
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        assert store.get_table_descriptions("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_isolated_by_source_connection_id(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table(_users_table(), source_connection_id=SOURCE_Y)
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            descriptions={"id": self._desc("x desc")},
        )
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SOURCE_Y,
            descriptions={"id": self._desc("y desc")},
        )
        x = store.get_table_descriptions("public", "users", source_connection_id=SOURCE_X)
        y = store.get_table_descriptions("public", "users", source_connection_id=SOURCE_Y)
        assert x["id"].text == "x desc"
        assert y["id"].text == "y desc"
        store.close()

    def test_write_descriptions_without_table_row_raises(self) -> None:
        import sqlite3

        store = SQLiteStore(":memory:")
        with pytest.raises(sqlite3.IntegrityError):
            store.write_table_descriptions(
                "public",
                "missing",
                source_connection_id=SOURCE_X,
                descriptions={"col": self._desc()},
            )
        store.close()

    def test_empty_descriptions_dict_clears(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_descriptions(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            descriptions={"id": self._desc()},
        )
        store.write_table_descriptions(
            "public", "users", source_connection_id=SOURCE_X, descriptions={}
        )
        assert store.get_table_descriptions("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()


class TestColumnEmbeddings:
    def _emb(self, vector: tuple[float, ...] = (0.1, 0.2, 0.3)) -> object:
        from schemabrain.core.embedding import ColumnEmbedding

        return ColumnEmbedding(
            vector=vector,
            model="BAAI/bge-small-en-v1.5",
            dimension=len(vector),
        )

    def test_get_returns_empty_dict_when_none_stored(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        assert store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_write_then_get_round_trip(self) -> None:
        from schemabrain.core.embedding import ColumnEmbedding

        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        embs = {
            "id": self._emb((0.1, 0.2, 0.3)),
            "email": self._emb((0.4, 0.5, 0.6)),
        }
        store.write_table_embeddings(
            "public", "users", source_connection_id=SOURCE_X, embeddings=embs
        )
        got = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X)
        assert set(got.keys()) == {"id", "email"}
        assert got["id"].vector == (
            pytest.approx(0.1),
            pytest.approx(0.2),
            pytest.approx(0.3),
        )
        assert got["email"].vector == (
            pytest.approx(0.4),
            pytest.approx(0.5),
            pytest.approx(0.6),
        )
        assert got["id"].model == "BAAI/bge-small-en-v1.5"
        assert got["id"].dimension == 3
        assert isinstance(got["id"], ColumnEmbedding)
        store.close()

    def test_write_replaces_existing(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            embeddings={"id": self._emb((1.0, 0.0, 0.0))},
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            embeddings={"id": self._emb((0.0, 1.0, 0.0))},
        )
        got = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X)
        assert got["id"].vector == (
            pytest.approx(0.0),
            pytest.approx(1.0),
            pytest.approx(0.0),
        )
        store.close()

    def test_delete_table_cascades_to_embeddings(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            embeddings={"id": self._emb()},
        )
        store.delete_table("public", "users", source_connection_id=SOURCE_X)
        assert store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_isolated_by_source_connection_id(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table(_users_table(), source_connection_id=SOURCE_Y)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            embeddings={"id": self._emb((1.0, 0.0, 0.0))},
        )
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_Y,
            embeddings={"id": self._emb((0.0, 1.0, 0.0))},
        )
        x = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X)
        y = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_Y)
        assert x["id"].vector == (
            pytest.approx(1.0),
            pytest.approx(0.0),
            pytest.approx(0.0),
        )
        assert y["id"].vector == (
            pytest.approx(0.0),
            pytest.approx(1.0),
            pytest.approx(0.0),
        )
        store.close()

    def test_write_embeddings_without_table_row_raises(self) -> None:
        import sqlite3

        store = SQLiteStore(":memory:")
        with pytest.raises(sqlite3.IntegrityError):
            store.write_table_embeddings(
                "public",
                "missing",
                source_connection_id=SOURCE_X,
                embeddings={"col": self._emb()},
            )
        store.close()

    def test_empty_embeddings_dict_clears(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            embeddings={"id": self._emb()},
        )
        store.write_table_embeddings(
            "public", "users", source_connection_id=SOURCE_X, embeddings={}
        )
        assert store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X) == {}
        store.close()

    def test_corrupt_blob_length_raises_clear_error(self) -> None:
        # Defensive: if a BLOB on disk has the wrong byte count for its
        # claimed dimension (database corruption, manual UPDATE, schema
        # drift), `get_table_embeddings` must surface it loudly rather
        # than returning a silently wrong-length vector. Public writers
        # can't produce this state — we have to inject through the
        # store's connection to simulate it.
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        # claimed dimension = 4 (16 bytes), actual blob = 4 bytes
        store._conn.execute(  # type: ignore[union-attr]
            "INSERT INTO column_embeddings "
            "(schema_name, table_name, column_name, source_connection_id, "
            "model, dimension, vector, embedded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("public", "users", "id", SOURCE_X, "m", 4, b"\x00\x00\x00\x00", 0),
        )
        store._conn.commit()  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="BLOB length 4 does not match expected 16 bytes"):
            store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X)
        store.close()

    def test_round_trip_preserves_float32_precision_within_tolerance(self) -> None:
        # We persist float32 BLOBs. Values that fit in float32 should round-trip
        # exactly; values that don't should round to the nearest float32. This
        # test pins the contract.
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        # 384-dim like the real BAAI/bge-small-en-v1.5 model.
        original = tuple(i / 1000.0 for i in range(384))
        store.write_table_embeddings(
            "public",
            "users",
            source_connection_id=SOURCE_X,
            embeddings={"id": self._embedding_of_dim(original)},
        )
        got = store.get_table_embeddings("public", "users", source_connection_id=SOURCE_X)
        assert got["id"].dimension == 384
        assert len(got["id"].vector) == 384
        for orig, restored in zip(original, got["id"].vector, strict=True):
            assert restored == pytest.approx(orig, abs=1e-6)
        store.close()

    def _embedding_of_dim(self, vector: tuple[float, ...]) -> object:
        from schemabrain.core.embedding import ColumnEmbedding

        return ColumnEmbedding(
            vector=vector,
            model="BAAI/bge-small-en-v1.5",
            dimension=len(vector),
        )


class TestForeignKeyBackReferences:
    """`get_foreign_keys_targeting` finds all FKs that REFERENCE a given
    column (incoming edges). Used by `describe_column` to surface "which
    tables join into me here?"
    """

    def test_returns_empty_when_no_fks_target_the_column(self) -> None:
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        # No other table references public.users.id yet.
        result = store.get_foreign_keys_targeting(
            "public", "users", "id", source_connection_id=SOURCE_X
        )
        assert result == []
        store.close()

    def test_finds_single_back_reference(self) -> None:
        # users + orders, where orders.user_id FK → users.id.
        store = SQLiteStore(":memory:")
        sid = SOURCE_X
        store.write_table(_users_table(), source_connection_id=sid)
        orders = Table(
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
                Column(
                    name="user_id",
                    table_name="orders",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="orders_user_id_fkey",
                    source_columns=("user_id",),
                    target_schema="public",
                    target_table="users",
                    target_columns=("id",),
                ),
            ),
        )
        store.write_table(orders, source_connection_id=sid)

        # users.id is referenced by orders.user_id.
        result = store.get_foreign_keys_targeting("public", "users", "id", source_connection_id=sid)
        assert len(result) == 1
        fk = result[0]
        assert fk.name == "orders_user_id_fkey"
        assert fk.source_qualified_name == "public.orders"
        assert fk.source_columns == ("user_id",)
        assert fk.target_columns == ("id",)
        store.close()

    def test_does_not_match_columns_with_overlapping_names_in_other_tables(
        self,
    ) -> None:
        # An FK targeting `customers.id` must NOT come back when querying
        # for `users.id` — column names alone are ambiguous; the join
        # match is on (target_schema, target_table, target_columns).
        store = SQLiteStore(":memory:")
        sid = SOURCE_X
        store.write_table(_users_table(), source_connection_id=sid)

        customers = Table(
            name="customers",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="customers",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
            ),
        )
        store.write_table(customers, source_connection_id=sid)

        invoices = Table(
            name="invoices",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="invoices",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="customer_id",
                    table_name="invoices",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="invoices_customer_id_fkey",
                    source_columns=("customer_id",),
                    target_schema="public",
                    target_table="customers",
                    target_columns=("id",),
                ),
            ),
        )
        store.write_table(invoices, source_connection_id=sid)

        # Querying users.id must NOT find the invoices→customers FK.
        result = store.get_foreign_keys_targeting("public", "users", "id", source_connection_id=sid)
        assert result == []
        # And vice versa — querying customers.id must find ONLY the
        # invoices FK, not anything pointing at users.
        result = store.get_foreign_keys_targeting(
            "public", "customers", "id", source_connection_id=sid
        )
        assert len(result) == 1
        assert result[0].name == "invoices_customer_id_fkey"
        store.close()

    def test_filters_by_source_connection_id(self) -> None:
        # FKs in a different source's catalog must not leak in.
        store = SQLiteStore(":memory:")
        store.write_table(_users_table(), source_connection_id=SOURCE_X)
        store.write_table(_users_table(), source_connection_id=SOURCE_Y)

        orders_x = Table(
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
                Column(
                    name="user_id",
                    table_name="orders",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=2,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="orders_user_id_fkey",
                    source_columns=("user_id",),
                    target_schema="public",
                    target_table="users",
                    target_columns=("id",),
                ),
            ),
        )
        store.write_table(orders_x, source_connection_id=SOURCE_X)

        # SOURCE_X sees the FK; SOURCE_Y doesn't (no orders table there).
        x = store.get_foreign_keys_targeting("public", "users", "id", source_connection_id=SOURCE_X)
        y = store.get_foreign_keys_targeting("public", "users", "id", source_connection_id=SOURCE_Y)
        assert len(x) == 1
        assert y == []
        store.close()

    def test_composite_fk_with_target_column_in_middle_position_matches(self) -> None:
        # An FK whose target_columns is ("org_id", "user_id") must come
        # back when querying either of those columns — composite-key
        # detection cannot rely on `target_columns[0] == col`.
        store = SQLiteStore(":memory:")
        sid = SOURCE_X
        store.write_table(_members_table(), source_connection_id=sid)

        # Synthetic table with a composite FK targeting org_members
        # (org_id, user_id).
        events = Table(
            name="events",
            schema_name="public",
            columns=(
                Column(
                    name="id",
                    table_name="events",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=1,
                    is_primary_key=True,
                ),
                Column(
                    name="member_org_id",
                    table_name="events",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=2,
                ),
                Column(
                    name="member_user_id",
                    table_name="events",
                    schema_name="public",
                    data_type="bigint",
                    nullable=False,
                    ordinal_position=3,
                ),
            ),
            foreign_keys=(
                ForeignKey(
                    name="events_member_fkey",
                    source_columns=("member_org_id", "member_user_id"),
                    target_schema="public",
                    target_table="org_members",
                    target_columns=("org_id", "user_id"),
                ),
            ),
        )
        store.write_table(events, source_connection_id=sid)

        # Should match either column.
        for col in ("org_id", "user_id"):
            result = store.get_foreign_keys_targeting(
                "public", "org_members", col, source_connection_id=sid
            )
            assert len(result) == 1, f"composite FK must match on column {col!r}"
            assert result[0].name == "events_member_fkey"
        store.close()
