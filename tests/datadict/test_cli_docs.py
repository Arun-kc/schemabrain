"""`schemabrain docs` CLI surface — end-to-end through `main`.

Covers the exit-code contract (0 render / 1 no-sources / 2 operational),
both output formats, and stdout-vs-file writing. The happy path asserts
the CLI emits byte-for-byte the committed golden, so the whole
build -> aggregate -> render -> write chain is exercised through argv.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.entity import Entity, SingleTableBinding
from schemabrain.core.models import Column, Table
from schemabrain.core.store import SQLiteStore
from schemabrain.datadict.demo_store import build_saas_dictionary_store

_GOLDEN = Path(__file__).parent / "golden" / "saas_dictionary.md"


def _min_table(name: str) -> Table:
    return Table(
        name=name,
        schema_name="public",
        columns=(
            Column(
                name="id",
                table_name=name,
                schema_name="public",
                data_type="bigint",
                nullable=False,
                ordinal_position=1,
                is_primary_key=True,
            ),
        ),
        foreign_keys=(),
    )


def _saas_store(tmp_path: Path) -> str:
    db = tmp_path / "schemabrain.db"
    build_saas_dictionary_store(db)
    return str(db)


def _two_source_store(tmp_path: Path) -> str:
    db = tmp_path / "multi.db"
    build_saas_dictionary_store(db)  # source 1 (SOURCE_ID)
    with SQLiteStore(db) as store:
        store.write_table(_min_table("other"), source_connection_id="second_source")
        store.write_entity(
            Entity(
                name="other",
                description="",
                binding=SingleTableBinding("public.other"),
                identity="id",
            ),
            source_connection_id="second_source",
        )
    return str(db)


def test_docs_missing_store_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["docs", "--store-path", str(tmp_path / "nope.db")])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""  # body channel stays clean
    assert "store not found" in captured.err


def test_docs_renders_markdown_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["docs", "--store-path", _saas_store(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    # The CLI path emits exactly the committed golden.
    assert captured.out == _GOLDEN.read_text(encoding="utf-8")


def test_docs_writes_to_out_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_file = tmp_path / "dict.md"
    rc = main(["docs", "--store-path", _saas_store(tmp_path), "--out", str(out_file)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""  # body went to the file, not stdout
    assert "wrote" in captured.err
    assert out_file.read_text(encoding="utf-8") == _GOLDEN.read_text(encoding="utf-8")


def test_docs_html_format_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["docs", "--store-path", _saas_store(tmp_path), "--format", "html"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("<!DOCTYPE html>")
    assert "<table>" in captured.out


def test_docs_no_sources_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty = tmp_path / "empty.db"
    with SQLiteStore(empty):  # creates an empty store file
        pass
    rc = main(["docs", "--store-path", str(empty)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no indexed sources" in captured.err


def test_docs_ambiguous_multi_source_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["docs", "--store-path", _two_source_store(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "sources indexed" in captured.err


def test_docs_source_and_url_env_conflict_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "docs",
            "--store-path",
            _saas_store(tmp_path),
            "--source",
            "postgresql+psycopg://u@localhost/db",
            "--url-env",
            "SOME_DB_URL",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    # Pin the reason, not just the code — distinguishes the conflict path
    # from the other rc=2 branches.
    assert "--url-env" in captured.err


def test_docs_schema_version_mismatch_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A store written by a different schemabrain version: SQLiteStore
    # raises SchemaVersionMismatchError on open; docs must catch it and
    # emit a guided error with exit 2 (parity with `inspect`).
    import sqlite3

    path = tmp_path / "stale.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO schemabrain_meta VALUES ('schema_version', '999')")
    conn.commit()
    conn.close()
    rc = main(["docs", "--store-path", str(path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "version" in captured.err.lower()
