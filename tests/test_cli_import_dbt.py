"""CLI tests for `schemabrain import dbt`.

The import-dbt CLI is the user-facing wrapper around the
`schemabrain.imports.dbt` driver (step 5). These tests pin the
contract end-to-end (the parser, mapper, verify helper, and driver
itself are tested in their own files).

Tests use a `_source_factory` callable injected at the
`_cmd_import_dbt` level — a documented private test seam, because
opening real Postgres for a CLI test isn't worth the testcontainers
weight at the unit-test tier.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from schemabrain.cli import _cmd_import_dbt, main
from schemabrain.connectors.errors import TableNotFoundError
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

_TEST_URL = "postgresql+psycopg://user:pw@localhost:5432/db"


# ----- fixtures -------------------------------------------------------------


class _FakeDataSource:
    def __init__(self, tables: Mapping[tuple[str, str], Table]) -> None:
        self._tables = dict(tables)

    def list_tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        return list(self._tables.keys())

    def get_table(self, name: str, schema: str) -> Table:
        try:
            return self._tables[(schema, name)]
        except KeyError as exc:
            raise TableNotFoundError(f"table {schema}.{name} not found") from exc

    def close(self) -> None:  # pragma: no cover — CLI never closes a fake
        pass

    def __enter__(self) -> _FakeDataSource:
        # The CLI uses the factory result as a context manager (mirrors
        # `PostgresDataSource`); the fake matches that shape.
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


def _live_column(
    name: str,
    *,
    table: str,
    nullable: bool = False,
    ordinal: int = 1,
    is_primary_key: bool = False,
) -> Column:
    return Column(
        name=name,
        table_name=table,
        schema_name="public",
        data_type="integer",
        nullable=nullable,
        ordinal_position=ordinal,
        is_primary_key=is_primary_key,
    )


def _live_table(name: str, *columns: Column) -> Table:
    return Table(name=name, schema_name="public", columns=columns, foreign_keys=())


def _write_minimal_manifest(
    tmp_path: Path,
    *,
    project: str = "demo_project",
    model_name: str = "customer_dim",
) -> Path:
    """Write a one-model manifest with primary_key identity to disk."""
    unique_id = f"model.{project}.{model_name}"
    body = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.8.0",
            "project_name": project,
            "adapter_type": "postgres",
        },
        "nodes": {
            unique_id: {
                "resource_type": "model",
                "unique_id": unique_id,
                "name": model_name,
                "database": "schemabrain_test",
                "schema": "public",
                "alias": model_name,
                "description": "",
                "columns": {
                    "id": {
                        "name": "id",
                        "data_type": "integer",
                        "constraints": [{"type": "primary_key"}],
                    },
                },
                "depends_on": {"nodes": []},
            },
        },
        "sources": {},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(body))
    return path


def _seed_store_with_table(tmp_path: Path, name: str = "customer_dim") -> Path:
    from schemabrain.cli import _make_source_id

    store_path = tmp_path / "store.db"
    table = _live_table(name, _live_column("id", table=name, nullable=False, is_primary_key=True))
    with SQLiteStore(store_path) as store:
        store.write_table(table, source_connection_id=_make_source_id(_TEST_URL))
    return store_path


def _factory_for(*table_pairs: tuple[str, Table]):
    """Build a `_source_factory` returning a `_FakeDataSource`."""
    tables = {("public", name): t for name, t in table_pairs}

    def factory(url: str) -> _FakeDataSource:
        return _FakeDataSource(tables)

    return factory


# ----- arg parsing ----------------------------------------------------------


class TestArgParsing:
    def test_missing_manifest_path_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["import", "dbt"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "manifest_path" in err or "arguments" in err

    def test_missing_source_and_url_env_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = _write_minimal_manifest(tmp_path)
        exit_code = main(["import", "dbt", str(manifest)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "source" in err.lower() or "url-env" in err.lower()

    def test_dry_run_and_report_are_independent_flags(self, tmp_path: Path) -> None:
        # Sanity: dry-run + --report can co-exist (preview + capture
        # the plan as JSON for CI inspection).
        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        report_path = tmp_path / "report.json"
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=True,
            report_path=str(report_path),
            _source_factory=_factory_for(("customer_dim", live)),
        )
        assert exit_code == 0
        assert report_path.exists()


# ----- manifest errors ------------------------------------------------------


class TestManifestErrors:
    def test_missing_manifest_file_exits_one_with_guided_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = _cmd_import_dbt(
            manifest_path=str(tmp_path / "does_not_exist.json"),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(tmp_path / "store.db"),
            dry_run=True,
            report_path=None,
            _source_factory=_factory_for(),
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "does_not_exist.json" in err

    def test_invalid_json_manifest_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "manifest.json"
        bad.write_text("{ not valid json")
        exit_code = _cmd_import_dbt(
            manifest_path=str(bad),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(tmp_path / "store.db"),
            dry_run=True,
            report_path=None,
            _source_factory=_factory_for(),
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "manifest" in err.lower()

    def test_unsupported_manifest_version_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        old = tmp_path / "manifest.json"
        old.write_text(
            json.dumps(
                {
                    "metadata": {
                        "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v7.json",
                        "project_name": "old_project",
                    },
                    "nodes": {},
                    "sources": {},
                }
            )
        )
        exit_code = _cmd_import_dbt(
            manifest_path=str(old),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(tmp_path / "store.db"),
            dry_run=True,
            report_path=None,
            _source_factory=_factory_for(),
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "v7" in err and "v10" in err


# ----- dry-run --------------------------------------------------------------


class TestDryRun:
    def test_dry_run_prints_summary_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=True,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        # Summary should mention the project name + at least one of
        # add/update/take_ownership counts.
        assert "demo_project" in out
        assert "1" in out  # one model to add

    def test_dry_run_does_not_write_to_store(self, tmp_path: Path) -> None:
        # Plan-only: store_state before == after dry-run.
        from schemabrain.cli import _make_source_id

        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=True,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        with SQLiteStore(store_path) as store:
            entity = store.get_entity(
                "customer_dim", source_connection_id=_make_source_id(_TEST_URL)
            )
        assert entity is None  # dry-run wrote nothing


# ----- apply ----------------------------------------------------------------


class TestApply:
    def test_apply_writes_entity_with_dbt_origin(self, tmp_path: Path) -> None:
        from schemabrain.cli import _make_source_id

        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        assert exit_code == 0
        with SQLiteStore(store_path) as store:
            entity = store.get_entity(
                "customer_dim", source_connection_id=_make_source_id(_TEST_URL)
            )
        assert entity is not None
        assert entity.origin == "dbt_import"

    def test_apply_prints_summary(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        out = capsys.readouterr().out
        # Summary names the import action — what got added/updated/etc.
        assert "added" in out.lower() or "imported" in out.lower()


# ----- --report -------------------------------------------------------------


class TestReport:
    def test_report_writes_json_with_plan_structure(self, tmp_path: Path) -> None:
        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        report_path = tmp_path / "report.json"
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=True,
            report_path=str(report_path),
            _source_factory=_factory_for(("customer_dim", live)),
        )
        assert report_path.exists()
        body = json.loads(report_path.read_text())
        # JSON shape covers the bucket counts the CI consumer wants.
        assert body["dbt_project_name"] == "demo_project"
        assert body["counts"]["to_add"] == 1
        assert body["counts"]["to_update"] == 0
        assert body["counts"]["to_take_ownership"] == 0
        assert body["counts"]["orphans"] == 0
        assert body["counts"]["skipped"] == 0


# ----- orphan + skip breadcrumbs --------------------------------------------


class TestStderrBreadcrumbs:
    def test_orphan_names_appear_in_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from schemabrain.cli import _make_source_id
        from schemabrain.core.entity import Entity, SingleTableBinding

        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        # Seed an extra dbt_import row that's NOT in the manifest →
        # the run should name it as an orphan in stderr.
        with SQLiteStore(store_path) as store:
            table = _live_table(
                "old_dim",
                _live_column("id", table="old_dim", is_primary_key=True),
            )
            store.write_table(table, source_connection_id=_make_source_id(_TEST_URL))
            store.write_entity(
                Entity(
                    name="old_dim",
                    description="legacy",
                    binding=SingleTableBinding(qualified_table="public.old_dim"),
                    identity="id",
                    origin="dbt_import",
                ),
                source_connection_id=_make_source_id(_TEST_URL),
            )

        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )
        _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        err = capsys.readouterr().err
        assert "old_dim" in err

    def test_non_model_resource_counts_named_in_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The parser counts metrics / snapshots / seeds / analyses /
        # operations / exposures. Non-zero counts surface in stdout so
        # the user sees which resource types were deferred to a future
        # release (metric import lands alongside the metric model).
        body = {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                "project_name": "demo_project",
            },
            "nodes": {
                "model.demo_project.customer_dim": {
                    "resource_type": "model",
                    "unique_id": "model.demo_project.customer_dim",
                    "name": "customer_dim",
                    "database": "schemabrain_test",
                    "schema": "public",
                    "alias": "customer_dim",
                    "description": "",
                    "columns": {
                        "id": {
                            "name": "id",
                            "constraints": [{"type": "primary_key"}],
                        },
                    },
                    "depends_on": {"nodes": []},
                },
                "metric.demo_project.revenue": {
                    "resource_type": "metric",
                    "unique_id": "metric.demo_project.revenue",
                },
                "snapshot.demo_project.audit": {
                    "resource_type": "snapshot",
                    "unique_id": "snapshot.demo_project.audit",
                },
                "seed.demo_project.country_codes": {
                    "resource_type": "seed",
                    "unique_id": "seed.demo_project.country_codes",
                },
                "analysis.demo_project.ad_hoc": {
                    "resource_type": "analysis",
                    "unique_id": "analysis.demo_project.ad_hoc",
                },
                "operation.demo_project.hook": {
                    "resource_type": "operation",
                    "unique_id": "operation.demo_project.hook",
                },
                "exposure.demo_project.dashboard": {
                    "resource_type": "exposure",
                    "unique_id": "exposure.demo_project.dashboard",
                },
            },
            "sources": {},
        }
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(body))
        store_path = _seed_store_with_table(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=True,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        out = capsys.readouterr().out
        # All 6 non-model resource types are non-zero, so all 6 appear
        # in the breadcrumb. Catches a regression where one branch is
        # silently dropped.
        assert "metrics=1" in out
        assert "snapshots=1" in out
        assert "seeds=1" in out
        assert "analyses=1" in out
        assert "operations=1" in out
        assert "exposures=1" in out

    def test_skipped_models_named_in_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A model with no identity-resolution tier → skipped. Run
        # continues; stderr names the skipped model + reason.
        body = {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                "project_name": "demo_project",
            },
            "nodes": {
                "model.demo_project.no_identity": {
                    "resource_type": "model",
                    "unique_id": "model.demo_project.no_identity",
                    "name": "no_identity",
                    "database": "schemabrain_test",
                    "schema": "public",
                    "alias": "no_identity",
                    "description": "",
                    "columns": {"email": {"name": "email"}},  # no PK
                    "depends_on": {"nodes": []},
                },
            },
            "sources": {},
        }
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(body))
        store_path = _seed_store_with_table(tmp_path)

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(),
        )
        # Skipped model + no other models = nothing imported; exit 0.
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "no_identity" in err
        assert "identity" in err.lower()


# ----- store path errors ----------------------------------------------------


class TestStorePathErrors:
    def test_unwritable_store_path_exits_two(self, tmp_path: Path) -> None:
        # `/dev/null/store.db` is unambiguously unwritable on POSIX
        # (parent is a character device, not a directory). SQLiteStore
        # raises OSError at open time → exit 2 (structural). Mirrors
        # the same path used by `entities apply` for the equivalent
        # case.
        manifest = _write_minimal_manifest(tmp_path)
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )
        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path="/dev/null/store.db",
            dry_run=False,
            report_path=None,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        assert exit_code == 2


# ----- Postgres connection failure ------------------------------------------


class TestOperationalErrorHandling:
    def test_postgres_connection_failure_routed_through_guided_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # When the live Postgres connection fails (wrong host, bad
        # password, timeout), SQLAlchemy raises OperationalError. The
        # CLI must surface a guided message (not a raw traceback) and
        # exit 2. Mirrors `_cmd_index` / `_cmd_serve` / `_cmd_mine_queries`.
        from sqlalchemy.exc import OperationalError

        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)

        class _ExplodingFactory:
            def __call__(self, url: str):
                # Construct a minimal OperationalError. The real
                # SQLAlchemy variant has 3 positional args (statement,
                # params, orig); the constructor handles None.
                raise OperationalError(
                    statement="", params=None, orig=Exception("connection refused")
                )

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=None,
            _source_factory=_ExplodingFactory(),
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        # The guided panel mentions the connection failure shape.
        assert (
            "connection" in err.lower()
            or "operational" in err.lower()
            or "could not" in err.lower()
        )


# ----- --report write failures ---------------------------------------------


class TestReportWriteFailures:
    def test_report_path_unwritable_exits_two_after_apply(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The report write happens AFTER the store write has
        # committed. An unwritable report path must NOT crash with a
        # traceback — exit 2 with a guided stderr message so CI
        # consumers can distinguish "report missing" from "import
        # failed".
        from schemabrain.cli import _make_source_id

        manifest = _write_minimal_manifest(tmp_path)
        store_path = _seed_store_with_table(tmp_path)
        # `/dev/null/...` is unambiguously unwritable.
        unwritable_report = "/dev/null/no_such_dir/report.json"
        live = _live_table(
            "customer_dim",
            _live_column("id", table="customer_dim", is_primary_key=True),
        )

        exit_code = _cmd_import_dbt(
            manifest_path=str(manifest),
            positional_url=_TEST_URL,
            url_env=None,
            store_path=str(store_path),
            dry_run=False,
            report_path=unwritable_report,
            _source_factory=_factory_for(("customer_dim", live)),
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "report" in err.lower()
        # Critical: the entity DID land — only the report write failed.
        with SQLiteStore(store_path) as store:
            entity = store.get_entity(
                "customer_dim", source_connection_id=_make_source_id(_TEST_URL)
            )
        assert entity is not None
        assert entity.origin == "dbt_import"
