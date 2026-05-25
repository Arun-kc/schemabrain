"""Regression coverage: `schemabrain serve` must emit a guided block
instead of a Python traceback when the local store is on a different
schema version.

Before this fix, Claude Desktop launched the bundled `serve` binary
against a store written by an older SchemaBrain version, hit
`SchemaVersionMismatchError` inside `SQLiteStore.__init__`, and let
the traceback bubble up to MCP stderr. Claude Desktop's UI just
showed "Server disconnected"; the operator had to dig into Claude
Desktop's per-server log file to find the actual error.

The fix copies the pattern `inspect` / `check` / `doctor` already
use: catch the exception in `_cmd_serve` and render via
`_render_guided(GuidedError(...))`. The remediation message is
identical across all four subcommands so operators only have to
learn it once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main


def _seed_wrong_version_store(path: Path) -> None:
    """Write a SQLite file at `path` with a schema_version that
    won't match the current `SCHEMA_VERSION` constant.

    Hand-crafting the minimal schema avoids importing `SQLiteStore`
    just to immediately corrupt it — this test asserts behavior on
    a store written by a previous code release, which is exactly
    what the user hit during the smoke. Using `"0"` keeps the
    fixture stable across future bumps of `SCHEMA_VERSION`.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO schemabrain_meta (key, value) VALUES (?, ?)",
            ("schema_version", "0"),
        )
        conn.commit()
    finally:
        conn.close()


class TestServeSchemaVersionMismatch:
    def test_emits_guided_block_not_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "stale.db"
        _seed_wrong_version_store(store_path)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
            ]
        )

        # Exit 2 matches the convention every other guided-block
        # error path uses; Claude Desktop logs the exit code so the
        # operator can grep for it.
        assert exit_code == 2

        captured = capsys.readouterr()
        # The guided block lives on stderr (MCP stdout is reserved
        # for protocol frames). Assert key remediation fragments
        # rather than the full block — the renderer style may
        # evolve, the contract is "operator sees the fix steps".
        assert "schema version" in captured.err
        assert "delete" in captured.err.lower() or "rm " in captured.err
        assert "schemabrain init" in captured.err
        # And critically: no Python traceback.
        assert "Traceback" not in captured.err
        assert "SchemaVersionMismatchError" not in captured.err or "Traceback" not in captured.err


def _seed_corrupted_store(path: Path) -> None:
    """Write a file that isn't valid SQLite (truncated header).

    SQLite refuses to open this with `DatabaseError: file is not a
    database`, mirroring the hard-shutdown / truncated-WAL / OS-level
    corruption case the regression case covers.
    """
    path.write_bytes(b"NOT_A_VALID_SQLITE_HEADER" + b"\x00" * 100)


class TestServeStoreCorrupted:
    """Regression coverage: a
    corrupted store file surfaces as `sqlite3.DatabaseError`, NOT as
    `OSError` or `SchemaVersionMismatchError`. Before this fix, the
    bare exception would bubble up to MCP stderr as a Python
    traceback — same UX cliff `SchemaVersionMismatchError` had
    before its fix.
    """

    def test_emits_guided_block_not_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store_path = tmp_path / "corrupted.db"
        _seed_corrupted_store(store_path)
        monkeypatch.setenv("FAKE_URL", "postgresql+psycopg://fake/serve")

        exit_code = cli_main(
            [
                "serve",
                "--url-env",
                "FAKE_URL",
                "--store-path",
                str(store_path),
                "--no-events",
                "--no-audit",
            ]
        )

        assert exit_code == 2

        captured = capsys.readouterr()
        # Block should mention the corruption symptom AND the
        # remediation. We deliberately don't pin the exact error
        # message text from sqlite3 — it varies by sqlite version.
        assert "corrupted" in captured.err.lower() or "not a database" in captured.err.lower()
        assert "schemabrain init" in captured.err
        # No traceback — the whole point of the fix.
        assert "Traceback" not in captured.err
