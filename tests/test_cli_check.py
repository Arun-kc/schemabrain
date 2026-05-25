"""Tests for `schemabrain check` CLI registration + dispatch.

Goals:

  1. The `check` subcommand parses the documented flag matrix:
     --source/--url-env (one required), --store-path, --json.
  2. Operational refusals (missing store, unset --url-env, both URL
     forms) exit with code 2 BEFORE the engine runs.
  3. Drift in the live source exits 1; clean state exits 0.
  4. `--json` writes the documented contract shape to stdout and
     suppresses the human render.
  5. The human-readable path writes to stderr so JSON pipelines can
     consume stdout cleanly in mixed-output scripts.

The CLI test uses a `PostgresDataSource` monkeypatch (the same
seam every other `index` / `joins suggest` test relies on) — the
fake source returns hand-crafted `Table` objects so the engine
exercises both healthy + drifted paths without a real Postgres.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from schemabrain.cli import main
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

# Matches `_make_source_id("postgresql+psycopg://fake/db")` — the
# canonical form expands the missing port to 5432 before hashing, so
# this is the SHA-256 prefix of the canonicalised URL, not the raw one.
SOURCE_ID = "0d8753a94d271485"


# ----- helpers --------------------------------------------------------------


def _make_table(schema: str, name: str, columns: list[tuple[str, bool]]) -> Table:
    cols = tuple(
        Column(
            name=c,
            table_name=name,
            schema_name=schema,
            data_type="text",
            nullable=not pk,
            ordinal_position=i + 1,
            is_primary_key=pk,
        )
        for i, (c, pk) in enumerate(columns)
    )
    return Table(name=name, schema_name=schema, columns=cols)


class _FakeSource:
    """A DataSource that returns pre-seeded tables; missing → TableNotFoundError.

    Carries an `_live_tables` class-level attribute so the
    monkeypatch can swap the live schema between tests without
    reaching through closures.
    """

    _live_tables: ClassVar[dict[tuple[str, str], Table]] = {}

    def __init__(self, url: str) -> None:
        pass

    def __enter__(self) -> _FakeSource:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        return list(type(self)._live_tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        try:
            return type(self)._live_tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"{schema}.{name}") from exc

    def close(self) -> None:
        pass


# ----- fixtures -------------------------------------------------------------


@pytest.fixture
def store_with_customer_entity(tmp_path: Path) -> Iterator[Path]:
    """A store containing one `customer` entity bound to public.customers."""
    path = tmp_path / "store.db"
    with SQLiteStore(path=path) as store:
        store.write_table(
            Table(
                schema_name="public",
                name="customers",
                columns=(
                    Column(
                        name="id",
                        table_name="customers",
                        schema_name="public",
                        data_type="int",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="email",
                        table_name="customers",
                        schema_name="public",
                        data_type="text",
                        nullable=True,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=SOURCE_ID,
        )
        store.write_entity(
            Entity(
                name="customer",
                description="A registered customer",
                binding=SingleTableBinding(qualified_table="public.customers"),
                identity="id",
            ),
            source_connection_id=SOURCE_ID,
        )
    yield path


@pytest.fixture(autouse=True)
def reset_fake_live(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset the class-level live-source state between tests."""
    monkeypatch.setattr(_FakeSource, "_live_tables", {}, raising=True)
    yield


# ----- argparse + dispatch --------------------------------------------------


class TestCheckParsing:
    def test_check_with_no_source_or_url_env_exits_two(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(["check", "--store-path", str(store_with_customer_entity)])
        assert exit_code == 2
        # The guided-error renderer emits to stderr.
        err = capsys.readouterr().err
        assert "--source" in err or "--url-env" in err

    def test_check_with_both_source_and_url_env_exits_two(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        monkeypatch.setenv("DB_URL", "postgresql+psycopg://fake/db")
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--url-env",
                "DB_URL",
                "--store-path",
                str(store_with_customer_entity),
            ]
        )
        assert exit_code == 2

    def test_check_with_unset_url_env_exits_two(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        monkeypatch.delenv("MISSING_DB_URL", raising=False)
        exit_code = main(
            [
                "check",
                "--url-env",
                "MISSING_DB_URL",
                "--store-path",
                str(store_with_customer_entity),
            ]
        )
        assert exit_code == 2

    def test_check_with_schema_version_mismatch_exits_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Hand-craft a schema-version-mismatched store; the check
        # command must surface the mismatch as exit 2, not crash.
        import sqlite3

        path = tmp_path / "store.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schemabrain_meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(path),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "schema" in err.lower()

    def test_check_with_unreachable_source_exits_two(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A DataSource whose constructor raises OperationalError must
        # be translated by the existing guided-error helper, not
        # propagated as an uncaught exception.
        from sqlalchemy.exc import OperationalError as SAOperationalError

        class _UnreachableSource:
            def __init__(self, url: str) -> None:
                raise SAOperationalError(
                    "could not connect", params=None, orig=Exception("refused")
                )

            def __enter__(self) -> _UnreachableSource:
                return self

            def __exit__(self, *_args: object) -> None:
                pass

            def close(self) -> None:
                pass

        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _UnreachableSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer_entity),
            ]
        )
        assert exit_code == 2

    def test_check_with_missing_store_exits_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        missing = tmp_path / "does-not-exist.db"
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(missing),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "store not found" in err


# ----- exit-code semantics --------------------------------------------------


class TestCheckExitCode:
    def test_clean_state_exits_zero(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            _FakeSource,
            "_live_tables",
            {
                ("public", "customers"): _make_table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
            },
        )
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer_entity),
            ]
        )
        assert exit_code == 0

    def test_drift_exits_one(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Live source has no `customers` table at all → table_missing drift.
        monkeypatch.setattr(_FakeSource, "_live_tables", {})
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer_entity),
            ]
        )
        assert exit_code == 1


# ----- output routing -------------------------------------------------------


class TestCheckOutputRouting:
    def test_json_mode_writes_to_stdout_contract_shape(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            _FakeSource,
            "_live_tables",
            {
                ("public", "customers"): _make_table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
            },
        )
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer_entity),
                "--json",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        parsed: dict[str, Any] = json.loads(captured.out)
        assert set(parsed.keys()) == {"drifts", "summary", "exit_code"}
        assert parsed["exit_code"] == 0
        assert parsed["summary"]["entities_total"] == 1
        assert parsed["summary"]["entities_healthy"] == 1
        # JSON mode suppresses the rich rendering on stderr.
        assert "SchemaBrain check" not in captured.err

    def test_human_mode_writes_to_stderr(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            _FakeSource,
            "_live_tables",
            {
                ("public", "customers"): _make_table(
                    "public", "customers", [("id", True), ("email", False)]
                ),
            },
        )
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer_entity),
            ]
        )
        captured = capsys.readouterr()
        # Human render lands on stderr.
        assert "SchemaBrain check" in captured.err
        # stdout is reserved for JSON output / pipe-clean payloads.
        assert captured.out == ""

    def test_human_render_handles_empty_store(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A store with no entities / metrics / joins should render an
        # "empty store" hint and exit 0 — not the "all healthy" line.
        path = tmp_path / "store.db"
        with SQLiteStore(path=path):
            pass  # Initialises schema, leaves it empty.
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(path),
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "No semantic-layer definitions" in err

    def test_drift_json_contains_drift_payload(
        self,
        store_with_customer_entity: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # table_missing drift
        monkeypatch.setattr(_FakeSource, "_live_tables", {})
        monkeypatch.setattr("schemabrain.cli.PostgresDataSource", _FakeSource)
        exit_code = main(
            [
                "check",
                "--source",
                "postgresql+psycopg://fake/db",
                "--store-path",
                str(store_with_customer_entity),
                "--json",
            ]
        )
        assert exit_code == 1
        parsed = json.loads(capsys.readouterr().out)
        assert len(parsed["drifts"]) == 1
        drift = parsed["drifts"][0]
        assert drift["def_kind"] == "entity"
        assert drift["def_name"] == "customer"
        assert drift["drift_kind"] == "table_missing"
