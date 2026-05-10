"""Tests for the schemabrain CLI."""

from pathlib import Path

import pytest

from schemabrain.cli import _canonical_url, _make_source_id, main
from schemabrain.core.store import SQLiteStore


class TestCanonicalUrl:
    def test_strips_credentials(self):
        with_creds = "postgresql://alice:s3cret@db.example.com:5432/mydb"
        without_creds = "postgresql://db.example.com:5432/mydb"
        assert _canonical_url(with_creds) == _canonical_url(without_creds)

    def test_normalizes_default_port(self):
        with_port = "postgresql://db.example.com:5432/mydb"
        no_port = "postgresql://db.example.com/mydb"
        assert _canonical_url(with_port) == _canonical_url(no_port)

    def test_normalizes_trailing_slash(self):
        with_slash = "postgresql://db.example.com:5432/mydb/"
        without_slash = "postgresql://db.example.com:5432/mydb"
        assert _canonical_url(with_slash) == _canonical_url(without_slash)

    def test_does_not_contain_credentials(self):
        url = "postgresql://alice:s3cret@db.example.com:5432/mydb"
        canonical = _canonical_url(url)
        assert "alice" not in canonical
        assert "s3cret" not in canonical

    def test_rejects_url_with_no_scheme(self):
        with pytest.raises(ValueError, match="no scheme"):
            _canonical_url("not-a-url")

    def test_rejects_unsupported_scheme(self):
        with pytest.raises(ValueError, match="Unsupported scheme"):
            _canonical_url("mysql://db.example.com:3306/mydb")

    def test_accepts_postgres_alias_scheme(self):
        # postgres:// is a common alias accepted by drivers
        assert _canonical_url("postgres://host/db").startswith("postgres://")

    def test_accepts_psycopg_driver_scheme(self):
        url = "postgresql+psycopg://host/db"
        assert _canonical_url(url).startswith("postgresql+psycopg://")


class TestMakeSourceId:
    def test_same_db_via_different_credentials_produces_same_id(self):
        a = "postgresql://alice:secret1@db.example.com:5432/mydb"
        b = "postgresql://bob:secret2@db.example.com:5432/mydb"
        assert _make_source_id(a) == _make_source_id(b)

    def test_default_port_and_explicit_5432_produce_same_id(self):
        a = "postgresql://db.example.com:5432/mydb"
        b = "postgresql://db.example.com/mydb"
        assert _make_source_id(a) == _make_source_id(b)

    def test_trailing_slash_produces_same_id(self):
        a = "postgresql://db.example.com/mydb"
        b = "postgresql://db.example.com/mydb/"
        assert _make_source_id(a) == _make_source_id(b)

    def test_different_databases_get_different_ids(self):
        a = "postgresql://db.example.com:5432/db_a"
        b = "postgresql://db.example.com:5432/db_b"
        assert _make_source_id(a) != _make_source_id(b)

    def test_different_hosts_get_different_ids(self):
        a = "postgresql://host1:5432/mydb"
        b = "postgresql://host2:5432/mydb"
        assert _make_source_id(a) != _make_source_id(b)

    def test_id_is_short_hex(self):
        url = "postgresql://db.example.com:5432/mydb"
        result = _make_source_id(url)
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_rejects_malformed_url(self):
        with pytest.raises(ValueError):
            _make_source_id("not-a-url")


class TestArgParsing:
    def test_no_args_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main([])
        assert exc.value.code != 0

    def test_unknown_subcommand_exits_nonzero(self):
        with pytest.raises(SystemExit) as exc:
            main(["nonexistent"])
        assert exc.value.code != 0

    def test_index_requires_url(self):
        with pytest.raises(SystemExit) as exc:
            main(["index"])
        assert exc.value.code != 0


class TestIndexCommandValidation:
    def test_malformed_url_returns_nonzero_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "not-a-url", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "error" in capsys.readouterr().err.lower()

    def test_unsupported_scheme_returns_nonzero_exit_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "mysql://db/test", "--store-path", str(store_path)])
        assert exit_code == 2
        assert "Unsupported scheme" in capsys.readouterr().err

    def test_warns_when_no_tables_indexed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ):
        """When list_tables returns empty, print a warning to stderr."""

        class _EmptySource:
            def __init__(self, url: str) -> None:
                pass

            def __enter__(self) -> "_EmptySource":
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def list_tables(self) -> list[tuple[str, str]]:
                return []

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _EmptySource)
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", "postgresql://fake/db", "--store-path", str(store_path)])
        assert exit_code == 0
        assert "no tables indexed" in capsys.readouterr().err


@pytest.mark.integration
class TestIndexEndToEnd:
    def test_index_populates_store_with_seeded_postgres(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        exit_code = main(["index", seeded_pg_url, "--store-path", str(store_path)])
        assert exit_code == 0

        with SQLiteStore(store_path) as store:
            indexed = sorted(store.list_tables())

        assert ("public", "users") in indexed
        assert ("public", "orgs") in indexed
        assert ("public", "org_members") in indexed
        assert ("audit", "events") in indexed

    def test_index_writes_correct_table_metadata(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        main(["index", seeded_pg_url, "--store-path", str(store_path)])

        source_id = _make_source_id(seeded_pg_url)
        with SQLiteStore(store_path) as store:
            users = store.get_table("public", "users", source_connection_id=source_id)

        assert users is not None
        assert users.primary_key_columns() == ("id",)
        col_names = {c.name for c in users.columns}
        assert col_names == {"id", "email", "created_at"}

    def test_index_is_idempotent_on_table_data(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        """Re-indexing the same database must produce byte-identical tables.

        Catches the bug class where re-index appends duplicate column rows
        instead of replacing them.
        """
        store_path = tmp_path / "schemabrain.db"
        source_id = _make_source_id(seeded_pg_url)

        main(["index", seeded_pg_url, "--store-path", str(store_path)])
        with SQLiteStore(store_path) as store:
            first_tables = sorted(store.list_tables())
            first_users = store.get_table("public", "users", source_connection_id=source_id)
            first_members = store.get_table("public", "org_members", source_connection_id=source_id)

        main(["index", seeded_pg_url, "--store-path", str(store_path)])
        with SQLiteStore(store_path) as store:
            second_tables = sorted(store.list_tables())
            second_users = store.get_table("public", "users", source_connection_id=source_id)
            second_members = store.get_table(
                "public", "org_members", source_connection_id=source_id
            )

        assert first_tables == second_tables
        assert first_users == second_users
        assert first_members == second_members
        # Spot-check that we didn't accumulate duplicate column rows
        assert first_users is not None
        assert len(first_users.columns) == 3

    def test_summary_does_not_leak_credentials(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # The seeded_pg_url contains test/test credentials
        store_path = tmp_path / "schemabrain.db"
        main(["index", seeded_pg_url, "--store-path", str(store_path)])
        captured = capsys.readouterr()
        # The default testcontainers user/password is "test"; ensure neither
        # the password nor "test:test" credential pair appears in the summary.
        assert "test:test" not in captured.err

    def test_index_prints_summary(
        self, seeded_pg_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        store_path = tmp_path / "schemabrain.db"
        main(["index", seeded_pg_url, "--store-path", str(store_path)])
        captured = capsys.readouterr()
        assert "Indexed" in captured.err
        assert "table" in captured.err
