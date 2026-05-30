"""CLI tests for `schemabrain policy show|apply|tag override|tag clear|tag list`."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from schemabrain.cli import (
    _cmd_policy_apply,
    _cmd_policy_show,
    _cmd_policy_tag_clear,
    _cmd_policy_tag_list,
    _cmd_policy_tag_override,
    _try_load_policy_yaml_block,
)
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

SRC = "test-source"


def _seed_minimal_store(path: Path, *, source_id: str = SRC) -> None:
    """Seed a store with one table + one classified column."""
    store = SQLiteStore(path)
    try:
        store.write_table(
            Table(
                name="users",
                schema_name="public",
                columns=(
                    Column(
                        name="id",
                        table_name="users",
                        schema_name="public",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                        is_primary_key=True,
                    ),
                    Column(
                        name="email",
                        table_name="users",
                        schema_name="public",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=2,
                    ),
                ),
            ),
            source_connection_id=source_id,
        )
        store.write_column_pii_tags(
            source_connection_id=source_id,
            qualified_table="public.users",
            tags={"email": ("pii", frozenset({"contact"}))},
        )
    finally:
        store.close()


# ---- _try_load_policy_yaml_block helper ------------------------------


class TestTryLoadPolicyYamlBlock:
    def test_returns_none_when_path_is_none(self) -> None:
        assert _try_load_policy_yaml_block(None) is None

    def test_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        assert _try_load_policy_yaml_block(str(tmp_path / "missing.yaml")) is None

    def test_loads_block_from_valid_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "pii_policy.yaml"
        path.write_text("version: 1\nblock:\n  - credential\n", encoding="utf-8")
        loaded = _try_load_policy_yaml_block(str(path))
        assert loaded == frozenset({"credential"})

    def test_returns_none_on_parse_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "pii_policy.yaml"
        path.write_text("version: 1\nblock: not-a-list\n", encoding="utf-8")
        result = _try_load_policy_yaml_block(str(path))
        assert result is None
        err = capsys.readouterr().err
        assert "cannot parse" in err


# ---- policy show -----------------------------------------------------


class TestPolicyShow:
    def test_shows_active_block_and_per_column_tags(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_show(
            store_path=str(store_path),
            policy_path=None,
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert SRC in out
        assert "credential" in out
        assert "email" in out
        assert "contact" in out
        assert "heuristic" in out

    def test_reads_yaml_when_policy_path_set(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        yaml_path = tmp_path / "pii_policy.yaml"
        yaml_path.write_text("version: 1\nblock:\n  - contact\n", encoding="utf-8")
        rc = _cmd_policy_show(
            store_path=str(store_path),
            policy_path=str(yaml_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "block source:  yaml" in out

    def test_errors_when_no_sources_in_store(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        SQLiteStore(store_path).close()
        rc = _cmd_policy_show(
            store_path=str(store_path),
            policy_path=None,
            positional_url=None,
            url_env=None,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "no indexed sources" in err

    def test_errors_when_multiple_sources_and_no_flag(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path, source_id="source-a")
        _seed_minimal_store(store_path, source_id="source-b")
        rc = _cmd_policy_show(
            store_path=str(store_path),
            policy_path=None,
            positional_url=None,
            url_env=None,
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "2 sources" in err

    def test_handles_no_tags_gracefully(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        # Seed table without tags so list_column_pii_tags_with_origin returns [].
        store = SQLiteStore(store_path)
        try:
            store.write_table(
                Table(
                    name="users",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="users",
                            schema_name="public",
                            data_type="BIGINT",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                    ),
                ),
                source_connection_id=SRC,
            )
        finally:
            store.close()
        rc = _cmd_policy_show(
            store_path=str(store_path),
            policy_path=None,
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "no PII tags recorded" in out


# ---- policy apply ----------------------------------------------------


class TestPolicyApply:
    def test_applies_overrides_to_store(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        yaml_path = tmp_path / "pii_policy.yaml"
        yaml_path.write_text(
            "version: 1\n"
            "block:\n  - credential\n"
            "column_overrides:\n"
            "  public.users.email:\n"
            "    sensitivity: internal\n"
            "    categories: []\n",
            encoding="utf-8",
        )
        rc = _cmd_policy_apply(
            yaml_path=str(yaml_path),
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "applied" in out
        assert "1 column override(s) persisted" in out
        # Verify the override actually landed.
        store = SQLiteStore(store_path)
        try:
            rows = store.list_column_pii_tags_with_origin(
                source_connection_id=SRC, origin="operator"
            )
        finally:
            store.close()
        assert len(rows) == 1
        assert rows[0][1] == "email"

    def test_errors_when_yaml_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_apply(
            yaml_path=str(tmp_path / "nonexistent.yaml"),
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "not found" in err

    def test_errors_on_malformed_yaml(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        yaml_path = tmp_path / "pii_policy.yaml"
        yaml_path.write_text("version: 1\n", encoding="utf-8")
        rc = _cmd_policy_apply(
            yaml_path=str(yaml_path),
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "block" in err

    def test_errors_when_path_is_directory(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_apply(
            yaml_path=str(tmp_path),
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "directory" in err


# ---- policy tag override ---------------------------------------------


class TestPolicyTagOverride:
    def test_writes_override_to_store(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_override(
            qualified_column="public.users.email",
            sensitivity="internal",
            categories_csv="",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "override written" in out
        # Verify in store.
        store = SQLiteStore(store_path)
        try:
            rows = store.list_column_pii_tags_with_origin(
                source_connection_id=SRC, origin="operator"
            )
        finally:
            store.close()
        assert len(rows) == 1
        assert rows[0][2] == "internal"

    def test_categories_csv_parsed_into_frozenset(
        self,
        tmp_path: Path,
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_override(
            qualified_column="public.users.email",
            sensitivity="pii",
            categories_csv="contact,location",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        store = SQLiteStore(store_path)
        try:
            rows = store.list_column_pii_tags_with_origin(
                source_connection_id=SRC, origin="operator"
            )
        finally:
            store.close()
        assert rows[0][3] == frozenset({"contact", "location"})

    def test_rejects_malformed_qualified_column(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_override(
            qualified_column="users.email",  # only 2 parts
            sensitivity="internal",
            categories_csv="",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "schema.table.column" in err

    def test_rejects_unknown_category(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_override(
            qualified_column="public.users.email",
            sensitivity="internal",
            categories_csv="not_a_real_category",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown" in err


# ---- policy tag clear ------------------------------------------------


class TestPolicyTagClear:
    def test_clears_existing_override(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        # Seed an override first.
        store = SQLiteStore(store_path)
        try:
            store.upsert_column_pii_tag_override(
                source_connection_id=SRC,
                qualified_table="public.users",
                column_name="email",
                sensitivity="internal",
                categories=frozenset(),
            )
        finally:
            store.close()
        rc = _cmd_policy_tag_clear(
            qualified_column="public.users.email",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "cleared" in out

    def test_returns_1_when_no_override_exists(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_clear(
            qualified_column="public.users.email",  # heuristic only, no override
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "no operator override" in err

    def test_rejects_malformed_qualified_column(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_clear(
            qualified_column="users.email",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 2


# ---- policy tag list -------------------------------------------------


class TestPolicyTagList:
    def test_lists_all_tags(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        rc = _cmd_policy_tag_list(
            origin=None,
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "public.users.email" in out
        assert "heuristic" in out

    def test_origin_filter_narrows_listing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        _seed_minimal_store(store_path)
        store = SQLiteStore(store_path)
        try:
            store.upsert_column_pii_tag_override(
                source_connection_id=SRC,
                qualified_table="public.payment_methods",
                column_name="card_number_last4",
                sensitivity="internal",
                categories=frozenset(),
            )
        finally:
            store.close()
        rc = _cmd_policy_tag_list(
            origin="operator",
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "card_number_last4" in out
        # The heuristic-tagged email should NOT appear in operator-only filter.
        assert "email" not in out

    def test_lists_zero_rows_cleanly(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "sb.db"
        # Seed source with table but no PII tags.
        store = SQLiteStore(store_path)
        try:
            store.write_table(
                Table(
                    name="users",
                    schema_name="public",
                    columns=(
                        Column(
                            name="id",
                            table_name="users",
                            schema_name="public",
                            data_type="BIGINT",
                            nullable=False,
                            ordinal_position=1,
                            is_primary_key=True,
                        ),
                    ),
                ),
                source_connection_id=SRC,
            )
        finally:
            store.close()
        rc = _cmd_policy_tag_list(
            origin=None,
            store_path=str(store_path),
            positional_url=None,
            url_env=None,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "no PII tags" in out


# ---- serve --policy-path wiring (unit, no real serve) ----------------


class TestServeReadsPolicyYaml:
    """Confirm `_cmd_serve` passes `pii_block` derived from the YAML
    when `--pii-block` is omitted. Exercises the wiring without
    booting a real MCP server."""

    def test_serve_reads_block_from_yaml_when_flag_omitted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from schemabrain.cli import _cmd_serve

        store_path = tmp_path / "sb.db"
        SQLiteStore(store_path).close()
        yaml_path = tmp_path / "pii_policy.yaml"
        yaml_path.write_text(
            "version: 1\nblock:\n  - contact\n  - location\n",
            encoding="utf-8",
        )

        captured: dict[str, object] = {}

        def _fake_run_stdio(*, pii_block, **kwargs) -> None:
            captured["pii_block"] = pii_block

        monkeypatch.setenv("FAKE_URL_ENV", "postgresql://u:p@h/db")
        monkeypatch.setattr("schemabrain.cli.run_stdio", _fake_run_stdio)
        # Avoid making real Postgres engine calls — _cmd_serve constructs
        # `sqlalchemy.create_engine` but doesn't connect until run_stdio
        # consumes the executor; the fake short-circuits before any DB
        # call. The url-resolution path needs to succeed though.
        with mock.patch("schemabrain.cli.fastembed_default", return_value=mock.Mock()):
            rc = _cmd_serve(
                positional_url=None,
                url_env="FAKE_URL_ENV",
                store_path=str(store_path),
                pii_block_csv=None,
                policy_path=str(yaml_path),
                no_audit=True,
                no_events=True,
            )

        assert rc == 0
        assert captured["pii_block"] == frozenset({"contact", "location"})
