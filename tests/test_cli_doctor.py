"""Tests for `schemabrain doctor` CLI registration + dispatch.

Goals:

  1. The `doctor` subcommand parses the documented flag matrix:
     no-args, --source/--url-env, --store-path, --host, --json.
  2. Exit code is the CheckResult's exit_code (0 on no-fail, 1 on
     any fail). Warnings never affect exit code.
  3. `--json` writes to stdout in the contract shape from Decision 7
     and suppresses the human-readable render.
  4. The human-readable path renders to stderr (so JSON tooling can
     pipe stdout without contamination); --json writes JSON to
     stdout.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from schemabrain.cli import main
from schemabrain.core.store import SQLiteStore


@pytest.fixture
def fresh_store(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "store.db"
    store = SQLiteStore(path=path)
    try:
        yield path
    finally:
        store.close()


class TestDoctorParsing:
    def test_doctor_with_no_args_runs_against_default_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Use --host manual so no host-config checks are added, and
        # point store-path at a tmp location so the default `.sb.db`
        # in cwd isn't touched.
        monkeypatch.chdir(tmp_path)
        exit_code = main(["doctor", "--host", "manual"])
        # No store, no source — store_schema_version warns; no fails.
        assert exit_code == 0

    def test_doctor_exit_code_is_one_when_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hand-craft a schema-version-mismatched store; doctor must
        # surface this as a fail and exit 1.
        import sqlite3

        path = tmp_path / "store.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schemabrain_meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        exit_code = main(["doctor", "--host", "manual", "--store-path", str(path)])
        assert exit_code == 1

    def test_doctor_with_source_runs_source_reachable_check(self, fresh_store: Path) -> None:
        # SQLite memory source is always reachable; doctor exits 0.
        exit_code = main(
            [
                "doctor",
                "--host",
                "manual",
                "--store-path",
                str(fresh_store),
                "--source",
                "sqlite:///:memory:",
            ]
        )
        assert exit_code == 0

    def test_doctor_with_url_env_uses_env_var(
        self,
        fresh_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_DB_URL", "sqlite:///:memory:")
        exit_code = main(
            [
                "doctor",
                "--host",
                "manual",
                "--store-path",
                str(fresh_store),
                "--url-env",
                "FAKE_DB_URL",
            ]
        )
        assert exit_code == 0

    def test_doctor_with_unset_url_env_exits_two(
        self,
        fresh_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MISSING_DB_URL", raising=False)
        exit_code = main(
            [
                "doctor",
                "--host",
                "manual",
                "--store-path",
                str(fresh_store),
                "--url-env",
                "MISSING_DB_URL",
            ]
        )
        # Operational refusal — exit 2 per Decision 9.
        assert exit_code == 2

    def test_doctor_with_both_source_and_url_env_exits_two(
        self,
        fresh_store: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("DB_URL", "sqlite:///:memory:")
        exit_code = main(
            [
                "doctor",
                "--host",
                "manual",
                "--store-path",
                str(fresh_store),
                "--source",
                "sqlite:///:memory:",
                "--url-env",
                "DB_URL",
            ]
        )
        assert exit_code == 2


class TestDoctorJsonOutput:
    def test_json_flag_writes_json_to_stdout(
        self,
        fresh_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "doctor",
                "--host",
                "manual",
                "--store-path",
                str(fresh_store),
                "--json",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        # JSON lands on stdout, parsable, with the contract shape.
        parsed = json.loads(captured.out)
        assert set(parsed.keys()) == {"checks", "summary", "exit_code"}
        assert parsed["exit_code"] == 0

    def test_json_flag_suppresses_human_render(
        self,
        fresh_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "doctor",
                "--host",
                "manual",
                "--store-path",
                str(fresh_store),
                "--json",
            ]
        )
        captured = capsys.readouterr()
        # No "Schema Brain doctor —" header (the human-rendered form).
        assert "Schema Brain doctor" not in captured.err
        assert "Schema Brain doctor" not in captured.out

    def test_human_render_writes_to_stderr(
        self,
        fresh_store: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Non-JSON mode renders to stderr so users can pipe stdout
        # if they want a clean output channel.
        main(["doctor", "--host", "manual", "--store-path", str(fresh_store)])
        captured = capsys.readouterr()
        assert "Schema Brain doctor" in captured.err
