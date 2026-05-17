"""CLI tests for `schemabrain entities list`.

`entities list` is the verification path after `entities apply`,
symmetric with `joins list` and `metrics list`. It exists so that
`apply` -> `list` is a single muscle-memory loop across all three
semantic-layer subgroups; before this CLI, users had to call
`list_entities` over MCP or read SQLite directly to see what entities
were defined.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import _make_source_id, main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore

_URL = "postgresql+psycopg://u:p@h/db"


def _seed_store_with_entity(store_path: Path) -> None:
    source_id = _make_source_id(_URL)
    with SQLiteStore(store_path) as store:
        store.write_table(
            Table(
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
            ),
            source_connection_id=source_id,
        )
        store.write_entity(
            Entity(
                name="customer",
                description="end-user buying things",
                binding=SingleTableBinding(qualified_table="public.users"),
                identity="id",
                origin="manual",
            ),
            source_connection_id=source_id,
        )


class TestEntitiesList:
    def test_list_empty_store(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path):
            pass
        exit_code = main(["entities", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        assert "no entities in the store" in capsys.readouterr().out

    def test_list_shows_applied_entity(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_entity(store_path)
        exit_code = main(["entities", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        # Single-line render mirrors `joins list` / `metrics list`.
        assert "customer" in out
        assert "table=public.users" in out
        assert "identity=id" in out
        assert "origin=manual" in out

    def test_list_with_source_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--source` filters to the matching source's entities; without
        the flag, lists across every source."""
        store_path = tmp_path / "store.db"
        _seed_store_with_entity(store_path)
        exit_code = main(["entities", "list", "--store-path", str(store_path), "--source", _URL])
        assert exit_code == 0
        assert "customer" in capsys.readouterr().out

    def test_list_with_unset_url_env_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unset `--url-env` should refuse with exit 2 — mirrors
        `joins list` / `metrics list` structural-error contract so the
        three surfaces don't drift."""
        store_path = tmp_path / "store.db"
        with SQLiteStore(store_path):
            pass
        exit_code = main(
            [
                "entities",
                "list",
                "--store-path",
                str(store_path),
                "--url-env",
                "SCHEMABRAIN_DOES_NOT_EXIST_FOR_TEST",
            ]
        )
        assert exit_code == 2

    def test_corrupt_entity_row_surfaces_clean_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hand-edited store with an invalid `identity` value (e.g.,
        embedded space) would otherwise crash `_row_to_entity` when it
        re-runs `Entity.__post_init__`'s `_IDENT_RE` check, and the
        ValueError would propagate as an unhandled traceback. Mirror
        `metrics list`'s defensive `except ValueError` so the user
        sees exit 2 + 'store appears corrupt' instead.

        The `identity` column has no CHECK constraint at the SQL
        level (it's `TEXT NOT NULL`), so an invalid value can sit in
        a store written by a tool that bypassed the Python writer.
        """
        import sqlite3

        store_path = tmp_path / "store.db"
        _seed_store_with_entity(store_path)
        c = sqlite3.connect(store_path)
        # `identity` allows any TEXT in SQL but the dataclass demands
        # identifier-shaped. A space-containing value passes SQL,
        # fails Entity.__post_init__.
        c.execute("UPDATE entities SET identity = 'has space'")
        c.commit()
        c.close()
        exit_code = main(["entities", "list", "--store-path", str(store_path)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "store appears corrupt" in err
