"""Tests for `schemabrain audit verify` and `schemabrain audit list`."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from schemabrain.audit.writer import AuditWriter, build_audit_row
from schemabrain.cli import main as cli_main
from schemabrain.core.store import SQLiteStore


class _FakeResponse:
    def __init__(self, status: str = "success") -> None:
        self.status = status


def _seed_store_with_audit_rows(
    store_path: Path,
    *,
    n: int = 3,
    tool_name: str = "describe_table",
    status: str = "success",
) -> None:
    """Initialise the store + audit table and write `n` audit rows."""
    SQLiteStore(store_path).close()
    writer = AuditWriter(store_path)
    try:
        for _ in range(n):
            draft = build_audit_row(
                tool_name=tool_name,
                source_connection_id="src1",
                response=_FakeResponse(status=status),
            )
            writer.write(draft)
    finally:
        writer.close()


class TestAuditVerifyClean:
    def test_clean_chain_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=3)
        exit_code = cli_main(["audit", "verify", "--store-path", str(store_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "intact" in out

    def test_empty_audit_table_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        # Just create the store; no audit writes.
        SQLiteStore(store_path).close()
        exit_code = cli_main(["audit", "verify", "--store-path", str(store_path)])
        assert exit_code == 0


class TestAuditVerifyTampered:
    def test_tampered_chain_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=3)

        # Tamper row 2's chain_hash directly via a connection that
        # drops the no-update trigger first (simulating an attacker
        # with file-level write).
        conn = sqlite3.connect(store_path)
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute(
                "UPDATE mcp_audit SET chain_hash = ? WHERE id = 2",
                (b"\xff" * 32,),
            )
            conn.commit()
        finally:
            conn.close()

        exit_code = cli_main(["audit", "verify", "--store-path", str(store_path)])
        assert exit_code == 1
        captured = capsys.readouterr()
        # Row 2's mismatch is reported on stdout; the count summary
        # lands on stderr.
        assert "row 2" in captured.out
        assert "mismatch" in captured.out
        assert "mismatch" in captured.err

    def test_full_flag_walks_past_first_mismatch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=5)

        conn = sqlite3.connect(store_path)
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute("UPDATE mcp_audit SET tool_name = 'tampered' WHERE id = 2")
            conn.execute("UPDATE mcp_audit SET tool_name = 'tampered' WHERE id = 4")
            conn.commit()
        finally:
            conn.close()

        exit_code = cli_main(["audit", "verify", "--store-path", str(store_path), "--full"])
        assert exit_code == 1
        out = capsys.readouterr().out
        # Both row 2 and row 4 should appear in the output.
        assert "row 2" in out
        assert "row 4" in out


class TestAuditVerifyStoreMissing:
    def test_missing_store_path_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nonexistent = tmp_path / "nope.db"
        exit_code = cli_main(["audit", "verify", "--store-path", str(nonexistent)])
        assert exit_code == 2
        assert "not found" in capsys.readouterr().err


class TestAuditList:
    def test_lists_all_rows_when_no_filters(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=3)
        exit_code = cli_main(["audit", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        # rich-rendered table contains the row count in the title.
        assert "mcp_audit" in out
        assert "describe_table" in out

    def test_empty_table_prints_friendly_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()
        exit_code = cli_main(["audit", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        assert "no audit rows" in capsys.readouterr().out

    def test_filter_by_tool_name(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()
        writer = AuditWriter(store_path)
        try:
            writer.write(
                build_audit_row(
                    tool_name="describe_table",
                    source_connection_id="src1",
                    response=_FakeResponse(status="success"),
                )
            )
            writer.write(
                build_audit_row(
                    tool_name="get_metric",
                    source_connection_id="src1",
                    response=_FakeResponse(status="success"),
                )
            )
        finally:
            writer.close()

        exit_code = cli_main(
            [
                "audit",
                "list",
                "--store-path",
                str(store_path),
                "--tool",
                "get_metric",
                "--json",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out.strip()
        # JSON-mode emits one line per row.
        rows = [json.loads(line) for line in out.split("\n")]
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "get_metric"

    def test_filter_by_status(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()
        writer = AuditWriter(store_path)
        try:
            writer.write(
                build_audit_row(
                    tool_name="describe_table",
                    source_connection_id="src1",
                    response=_FakeResponse(status="success"),
                )
            )
            writer.write(
                build_audit_row(
                    tool_name="describe_table",
                    source_connection_id="src1",
                    response=_FakeResponse(status="error"),
                )
            )
        finally:
            writer.close()

        exit_code = cli_main(
            [
                "audit",
                "list",
                "--store-path",
                str(store_path),
                "--status",
                "error",
                "--json",
            ]
        )
        assert exit_code == 0
        rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
        assert len(rows) == 1
        assert rows[0]["status"] == "error"

    def test_filter_by_since(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=2)
        # --since 1h captures everything just written.
        exit_code = cli_main(
            [
                "audit",
                "list",
                "--store-path",
                str(store_path),
                "--since",
                "1h",
                "--json",
            ]
        )
        assert exit_code == 0
        rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
        assert len(rows) == 2

    def test_invalid_since_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        exit_code = cli_main(
            [
                "audit",
                "list",
                "--store-path",
                str(store_path),
                "--since",
                "not-a-time",
            ]
        )
        assert exit_code == 2

    def test_limit_caps_rows(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=5)
        exit_code = cli_main(
            [
                "audit",
                "list",
                "--store-path",
                str(store_path),
                "--limit",
                "2",
                "--json",
            ]
        )
        assert exit_code == 0
        rows = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n")]
        assert len(rows) == 2

    def test_missing_store_path_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nonexistent = tmp_path / "nope.db"
        exit_code = cli_main(["audit", "list", "--store-path", str(nonexistent)])
        assert exit_code == 2
        assert "not found" in capsys.readouterr().err

    def test_json_mode_includes_fingerprint_hex(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        cli_main(["audit", "list", "--store-path", str(store_path), "--json"])
        out = capsys.readouterr().out.strip()
        row = json.loads(out)
        assert "fingerprint" in row
        assert len(row["fingerprint"]) == 64
        assert row["fingerprint_version"] == "fp-v1"
