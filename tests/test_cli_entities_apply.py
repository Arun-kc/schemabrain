"""CLI tests for `schemabrain entities apply`.

The load CLI is a thin orchestration layer: parse YAML → resolve
source → write to store → print status. These tests pin the contract
end-to-end (the YAML parser, store writer, and dbt-guard behaviour
itself are tested in their own files).

This is the non-interactive loader. The interactive LLM-suggest
review UX ships in a follow-up PR alongside the suggest pipeline;
`entities apply` itself reads a file and writes a row, nothing more.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

_USERS_YAML = """\
version: 1
name: customer
description: A registered shopper
binding:
  single_table: public.users
identity: id
"""

_DBT_USERS_YAML = """\
version: 1
name: customer
description: A registered shopper
binding:
  single_table: public.users
identity: id
origin: dbt_import
"""

_MULTI_TABLE_YAML = """\
version: 1
name: user
binding:
  multi_table: public.users + public.profiles
identity: id
"""


# ----- helpers ---------------------------------------------------------------


def _users_table() -> Table:
    return Table(
        name="users",
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name="users",
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
    )


def _seed_store(store_path: Path, url: str) -> None:
    """Pre-seed the store with `public.users` so entity FK is satisfied.

    `_make_source_id(url)` is deterministic and stable across calls, so
    the seed and the subsequent `entities apply` invocation agree on
    the source_connection_id without us having to thread it through.
    """
    from schemabrain.cli import _make_source_id

    with SQLiteStore(store_path) as store:
        store.write_table(_users_table(), source_connection_id=_make_source_id(url))


def _write_yaml(tmp_path: Path, content: str, name: str = "customer.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content)
    return path


_TEST_URL = "postgresql+psycopg://user:pw@localhost:5432/db"


# ----- arg parsing -----------------------------------------------------------


class TestArgParsing:
    def test_missing_yaml_path_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse calls sys.exit(2) directly when a required positional
        # is missing — it never returns into `main()`'s control flow.
        with pytest.raises(SystemExit) as exc_info:
            main(["entities", "apply"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "yaml_path" in err or "arguments" in err

    def test_missing_source_and_url_env_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)
        exit_code = main(["entities", "apply", str(yaml_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "--url-env" in err

    def test_missing_action_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        """`schemabrain entities` without an action is a usage error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["entities"])
        assert exc_info.value.code == 2

    def test_malformed_source_url_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                "not-a-url",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2

    def test_directory_path_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Passing a directory where a YAML file is expected fails fast."""
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        # tmp_path is itself a directory.
        exit_code = main(
            [
                "entities",
                "apply",
                str(tmp_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "directory" in err.lower()

    def test_unwritable_store_path_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pointing --store-path at an unwritable location surfaces a
        guided error and exits 2 (mirrors `serve` / `mine-queries`)."""
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)
        # `/dev/full` doesn't behave consistently across platforms;
        # instead point at a path whose parent doesn't exist AND can't
        # be created (a non-directory file used as a parent).
        not_a_dir = tmp_path / "not_a_dir"
        not_a_dir.write_text("placeholder")
        bad_store = not_a_dir / "store.db"
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(bad_store),
            ]
        )
        assert exit_code == 2

    def test_non_integrity_database_error_exits_two(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A non-Integrity `sqlite3.DatabaseError` (disk full, WAL
        checkpoint failure, CHECK constraint trip on a corrupted
        store) must surface as a guided error with exit 2, not a
        raw traceback."""
        import sqlite3 as sqlite3_module

        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)

        def raise_db_error(self: object, entity: object, **kwargs: object) -> None:
            raise sqlite3_module.DatabaseError("simulated WAL checkpoint failure")

        monkeypatch.setattr(SQLiteStore, "write_entity", raise_db_error)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "WAL checkpoint" in err or "store-level" in err


# ----- happy path ------------------------------------------------------------


class TestHappyPath:
    def test_applies_valid_yaml_and_prints_confirmation(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)

        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "customer" in out

    def test_idempotent_reapply_of_manual_entity(self, tmp_path: Path) -> None:
        """Re-running `entities apply` on the same YAML must succeed."""
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)

        argv = [
            "entities",
            "apply",
            str(yaml_path),
            "--source",
            _TEST_URL,
            "--store-path",
            str(store_path),
        ]
        assert main(argv) == 0
        assert main(argv) == 0

    def test_writes_to_store_using_url_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same flow via --url-env (the preferred credential path)."""
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)
        monkeypatch.setenv("SB_TEST_URL", _TEST_URL)

        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--url-env",
                "SB_TEST_URL",
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0


# ----- YAML parse errors -----------------------------------------------------


class TestParseErrors:
    def test_missing_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        exit_code = main(
            [
                "entities",
                "apply",
                str(tmp_path / "ghost.yaml"),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "ghost.yaml" in err or "not found" in err.lower()

    def test_multi_table_yaml_exits_one_with_v2_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        yaml_path = _write_yaml(tmp_path, _MULTI_TABLE_YAML)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "v2" in err.lower() or "multi_table" in err

    def test_missing_required_field_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        # Missing version field.
        bad = "name: customer\nbinding:\n  single_table: public.users\nidentity: id\n"
        yaml_path = _write_yaml(tmp_path, bad)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 1


# ----- store-side errors -----------------------------------------------------


class TestStoreErrors:
    def test_entity_binding_to_unknown_table_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An entity referencing a table that isn't indexed yet should
        surface as a guided error pointing at `schemabrain index`."""
        store_path = tmp_path / "store.db"
        # Do NOT seed any tables — the FK will fail.
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "index" in err.lower()

    def test_overwriting_dbt_import_with_manual_exits_one(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The dbt-owned write guard must surface at the CLI as a clear
        error pointing the user at the upstream dbt model."""
        from schemabrain.cli import _make_source_id

        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        source_id = _make_source_id(_TEST_URL)

        # Pre-write the dbt_import entity directly.
        with SQLiteStore(store_path) as store:
            store.write_entity(
                Entity(
                    name="customer",
                    description="A dbt-owned customer",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="dbt_import",
                ),
                source_connection_id=source_id,
            )

        # Now try to apply a manual YAML — should fail.
        yaml_path = _write_yaml(tmp_path, _USERS_YAML)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "dbt" in err.lower()

    def test_dbt_import_yaml_overwriting_dbt_import_succeeds(self, tmp_path: Path) -> None:
        """Idempotent re-import: dbt_import → dbt_import is allowed."""
        from schemabrain.cli import _make_source_id

        store_path = tmp_path / "store.db"
        _seed_store(store_path, _TEST_URL)
        source_id = _make_source_id(_TEST_URL)

        with SQLiteStore(store_path) as store:
            store.write_entity(
                Entity(
                    name="customer",
                    description="A dbt-owned customer",
                    binding=SingleTableBinding(qualified_table="public.users"),
                    identity="id",
                    origin="dbt_import",
                ),
                source_connection_id=source_id,
            )

        # Re-apply via a YAML that itself declares dbt_import.
        yaml_path = _write_yaml(tmp_path, _DBT_USERS_YAML)
        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_path),
                "--source",
                _TEST_URL,
                "--store-path",
                str(store_path),
            ]
        )
        assert exit_code == 0


_ORDERS_YAML = """\
version: 1
name: order
description: An order placed by a customer
binding:
  single_table: public.orders
identity: id
"""


def _orders_table() -> Table:
    return Table(
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
        ),
    )


def _seed_store_with_both_tables(store_path: Path, url: str) -> None:
    """Seed both `public.users` AND `public.orders` so multi-file
    apply tests can land a customer entity AND an order entity in
    one call."""
    from schemabrain.cli import _make_source_id

    source_id = _make_source_id(url)
    with SQLiteStore(store_path) as store:
        store.write_table(_users_table(), source_connection_id=source_id)
        store.write_table(_orders_table(), source_connection_id=source_id)


class TestMultiPath:
    """Regression tests for the post-PR-#65 smoke finding: a shell glob
    expansion (`schemabrain entities apply dir/*.yaml`) crashed with
    `unrecognized arguments` because the CLI accepted only one
    positional yaml_path. Fix: `nargs="+"` + per-file failure
    aggregation, mirroring `joins apply` / `metrics apply`."""

    _MULTI_URL = "postgresql+psycopg://u:p@h:5432/db"

    def test_apply_multiple_files_via_shell_glob_expansion(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Shell glob `dir/*.yaml` expands to multiple positional args.
        # Before the fix, only the first was accepted and the rest
        # raised "unrecognized arguments". Now: all land in one call.
        store_path = tmp_path / "store.db"
        _seed_store_with_both_tables(store_path, self._MULTI_URL)
        users_yaml = tmp_path / "customer.yaml"
        users_yaml.write_text(_USERS_YAML, encoding="utf-8")
        orders_yaml = tmp_path / "order.yaml"
        orders_yaml.write_text(_ORDERS_YAML, encoding="utf-8")

        exit_code = main(
            [
                "entities",
                "apply",
                str(users_yaml),
                str(orders_yaml),
                "--source",
                self._MULTI_URL,
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied entity: customer" in out
        assert "applied entity: order" in out

    def test_apply_directory_loads_every_yaml_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Mirror of the joins-side directory test.
        store_path = tmp_path / "store.db"
        _seed_store_with_both_tables(store_path, self._MULTI_URL)
        yaml_dir = tmp_path / "entities"
        yaml_dir.mkdir()
        (yaml_dir / "customer.yaml").write_text(_USERS_YAML, encoding="utf-8")
        (yaml_dir / "order.yaml").write_text(_ORDERS_YAML, encoding="utf-8")
        (yaml_dir / "ignored.txt").write_text("not yaml", encoding="utf-8")

        exit_code = main(
            [
                "entities",
                "apply",
                str(yaml_dir),
                "--source",
                self._MULTI_URL,
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied entity: customer" in out
        assert "applied entity: order" in out

    def test_partial_failure_aggregates_and_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # One good YAML + one bad YAML → good lands, bad reports,
        # exit code 1. Verifies the new failure-aggregation loop.
        store_path = tmp_path / "store.db"
        _seed_store_with_both_tables(store_path, self._MULTI_URL)
        users_yaml = tmp_path / "customer.yaml"
        users_yaml.write_text(_USERS_YAML, encoding="utf-8")
        bad_yaml = tmp_path / "broken.yaml"
        bad_yaml.write_text("not: a: valid: entity", encoding="utf-8")

        exit_code = main(
            [
                "entities",
                "apply",
                str(users_yaml),
                str(bad_yaml),
                "--source",
                self._MULTI_URL,
                "--store-path",
                str(store_path),
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "applied entity: customer" in captured.out
        assert "broken.yaml" in captured.err

    def test_mixed_file_and_directory_args(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `entities apply file.yaml dir/` — one explicit file plus
        # one directory. Both should apply.
        store_path = tmp_path / "store.db"
        _seed_store_with_both_tables(store_path, self._MULTI_URL)
        users_yaml = tmp_path / "customer.yaml"
        users_yaml.write_text(_USERS_YAML, encoding="utf-8")
        more_dir = tmp_path / "more"
        more_dir.mkdir()
        (more_dir / "order.yaml").write_text(_ORDERS_YAML, encoding="utf-8")

        exit_code = main(
            [
                "entities",
                "apply",
                str(users_yaml),
                str(more_dir),
                "--source",
                self._MULTI_URL,
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "applied entity: customer" in out
        assert "applied entity: order" in out

    def test_duplicate_paths_dedupe(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `entities apply file.yaml file.yaml` — should apply once,
        # not twice. Dedupe is keyed on resolved absolute path.
        store_path = tmp_path / "store.db"
        _seed_store_with_both_tables(store_path, self._MULTI_URL)
        users_yaml = tmp_path / "customer.yaml"
        users_yaml.write_text(_USERS_YAML, encoding="utf-8")

        exit_code = main(
            [
                "entities",
                "apply",
                str(users_yaml),
                str(users_yaml),
                "--source",
                self._MULTI_URL,
                "--store-path",
                str(store_path),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 0
        # Only one apply line — dedupe worked.
        assert out.count("applied entity: customer") == 1

    def test_non_yaml_extension_reported_as_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `entities apply notes.txt customer.yaml` — the txt path
        # surfaces in the failure summary; the yaml still applies.
        store_path = tmp_path / "store.db"
        _seed_store_with_both_tables(store_path, self._MULTI_URL)
        users_yaml = tmp_path / "customer.yaml"
        users_yaml.write_text(_USERS_YAML, encoding="utf-8")
        txt = tmp_path / "notes.txt"
        txt.write_text("not yaml", encoding="utf-8")

        exit_code = main(
            [
                "entities",
                "apply",
                str(txt),
                str(users_yaml),
                "--source",
                self._MULTI_URL,
                "--store-path",
                str(store_path),
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "applied entity: customer" in captured.out
        assert "notes.txt" in captured.err
        assert "not a `.yaml`" in captured.err
