"""Tests for `schemabrain audit verify` and `schemabrain audit list`."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabrain.audit.writer import AuditWriter, build_audit_row
from schemabrain.cli import _format_audit_occurred_at
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

    def test_multi_fingerprint_version_renders_informational_line(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A store that crossed a fingerprint-version bump renders
        a yellow `i N fingerprint versions present` line instead
        of the green `✓ fingerprint version consistent` claim. This
        is not a chain-integrity failure — `walk_chain` still
        returns zero mismatches because each row hashes correctly
        against its own canonical bytes — but the audit-verify
        renderer must surface the version diversity so the operator
        knows the deployment has crossed a bump.

        Seeds the store with rows from two distinct
        FINGERPRINT_VERSION values by monkeypatching the writer's
        binding between writes. Verifies exit code 0 (chain intact)
        AND the informational line appears.
        """
        store_path = tmp_path / "store.db"
        SQLiteStore(store_path).close()

        # First half: default "fp-v1" rows from the real writer.
        writer = AuditWriter(store_path)
        try:
            for _ in range(2):
                draft = build_audit_row(
                    tool_name="describe_table",
                    source_connection_id="src1",
                    response=_FakeResponse(),
                )
                writer.write(draft)
        finally:
            writer.close()

        # Second half: simulate a deployment crossing a bump by
        # repointing the writer's `FINGERPRINT_VERSION` binding
        # before opening a new writer. Re-opens the same store so
        # the chain continues from row 2; the new rows hash with
        # the new version stamped into the canonical input.
        monkeypatch.setattr("schemabrain.audit.writer.FINGERPRINT_VERSION", "fp-v2-test")
        writer = AuditWriter(store_path)
        try:
            for _ in range(2):
                draft = build_audit_row(
                    tool_name="describe_table",
                    source_connection_id="src1",
                    response=_FakeResponse(),
                )
                writer.write(draft)
        finally:
            writer.close()

        exit_code = cli_main(["audit", "verify", "--store-path", str(store_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        # Primary claim line still renders — chain is intact.
        assert "intact" in out
        # Informational line for the multi-version case — the
        # `_render_audit_chain_intact` `else` branch.
        assert "2 fingerprint versions present" in out

    def test_schema_drift_warns_but_still_verifies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`audit verify` must still walk the chain on a store with a
        drifted schema_version — the chain-hash format is stable across
        schema revisions — but warn so the user knows the surrounding
        column shape may not match the current code's expectations."""
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=2)
        c = sqlite3.connect(store_path)
        c.execute("UPDATE schemabrain_meta SET value = '99' WHERE key = 'schema_version'")
        c.commit()
        c.close()
        exit_code = cli_main(["audit", "verify", "--store-path", str(store_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "intact" in captured.out
        assert "warning:" in captured.err
        assert "schema_version" in captured.err


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

    def test_corrupt_db_file_returns_two_with_clean_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-SQLite file at the store path used to raise an
        unhandled traceback; the fix returns exit 2 with a clean
        error message."""
        bogus = tmp_path / "garbage.db"
        bogus.write_bytes(b"not a sqlite file" * 100)
        exit_code = cli_main(["audit", "verify", "--store-path", str(bogus)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "cannot open" in err or "SQLite" in err


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

    def test_negative_limit_rejected_with_clean_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A user passing `--limit -1` would historically be silently
        translated to "no limit" by SQLite (its LIMIT clause treats any
        negative value as unlimited), so a caller could pass `-1` and
        exfiltrate every audit row when ostensibly asking for one. Gate
        at argparse with a clear usage error.

        `argparse` raises `SystemExit(2)` on `type=` rejection (it's a
        parser-time error, not a handler-time refusal), so the test
        catches `SystemExit` rather than reading a return code — same
        pattern used elsewhere when arguments fail validation pre-dispatch.
        """
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=3)
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["audit", "list", "--store-path", str(store_path), "--limit", "-1"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "-1" in err
        assert "non-negative" in err.lower()

    def test_zero_limit_accepted_as_empty_result(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--limit 0` is a legitimate "show nothing" — the regression
        gate must accept it and emit an empty result, not error."""
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=3)
        exit_code = cli_main(
            ["audit", "list", "--store-path", str(store_path), "--limit", "0", "--json"]
        )
        assert exit_code == 0
        assert capsys.readouterr().out.strip() == ""

    def test_missing_store_path_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        nonexistent = tmp_path / "nope.db"
        exit_code = cli_main(["audit", "list", "--store-path", str(nonexistent)])
        assert exit_code == 2
        assert "not found" in capsys.readouterr().err

    def test_schema_drift_warns_but_proceeds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A store written by a future schemabrain (or a tampered meta
        row) should produce a stderr warning, NOT silently render rows
        whose column shape the current code can't trust.

        Warn-and-proceed: exit code 0, stderr contains the warning,
        and the rows still render (operators inspecting a frozen audit
        from a newer install must still be able to walk it)."""
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        # Simulate drift: bump the recorded schema_version far past
        # anything the current build will ever ship.
        c = sqlite3.connect(store_path)
        c.execute("UPDATE schemabrain_meta SET value = '99' WHERE key = 'schema_version'")
        c.commit()
        c.close()
        exit_code = cli_main(["audit", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "warning:" in captured.err
        assert "schema_version" in captured.err
        assert "'99'" in captured.err

    def test_missing_meta_row_warns_with_distinct_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When `schema_version` key is absent from `schemabrain_meta`
        but the audit table is otherwise readable, the helper should
        warn rather than silently proceed — the operator can't know
        whether the column shape is trustworthy."""
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        c = sqlite3.connect(store_path)
        c.execute("DELETE FROM schemabrain_meta WHERE key = 'schema_version'")
        c.commit()
        c.close()
        exit_code = cli_main(["audit", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "warning:" in err
        assert "no schema_version record" in err

    def test_schema_drift_warning_bounded_on_giant_value(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A crafted store with a multi-MB `schema_version` value would
        otherwise flood stderr. The helper caps the echoed value so the
        warning stays bounded."""
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        c = sqlite3.connect(store_path)
        attack = "a" * 50_000
        c.execute("UPDATE schemabrain_meta SET value = ? WHERE key = 'schema_version'", (attack,))
        c.commit()
        c.close()
        exit_code = cli_main(["audit", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        err = capsys.readouterr().err
        # The full 50 KB payload must not survive into the warning.
        assert len(err) < 2_000

    def test_no_schema_drift_warning_on_normal_store(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A store at the current schema version must not emit the drift
        warning — otherwise every healthy run looks like an incident."""
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        exit_code = cli_main(["audit", "list", "--store-path", str(store_path)])
        assert exit_code == 0
        assert "warning:" not in capsys.readouterr().err

    def test_corrupt_db_file_returns_two_with_clean_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bogus = tmp_path / "garbage.db"
        bogus.write_bytes(b"not a sqlite file" * 100)
        exit_code = cli_main(["audit", "list", "--store-path", str(bogus)])
        assert exit_code == 2
        err = capsys.readouterr().err
        assert "cannot open" in err or "SQLite" in err

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


class TestAuditListRendering:
    """Renderer-side coverage for the rich-table polish."""

    def test_pii_column_renders_in_table(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        cli_main(["audit", "list", "--store-path", str(store_path)])
        out = capsys.readouterr().out
        assert "pii" in out

    def test_empty_pii_shows_none_placeholder(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Default seeded rows have empty pii_categories — the (none)
        # placeholder must appear so empty cells are not silently blank.
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        cli_main(["audit", "list", "--store-path", str(store_path)])
        out = capsys.readouterr().out
        assert "(none)" in out

    def test_fingerprint_displays_sixteen_chars(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force a wide Console so the 16-char fingerprint cell does
        # not wrap when pytest captures stdout (no real TTY ->
        # Rich's default is 80 cols, which forces fold-wrapping on the
        # 7-column table). Real terminals are wider.
        from rich.console import Console as _RichConsole

        original_init = _RichConsole.__init__

        def wide_init(self: _RichConsole, *args: object, **kwargs: object) -> None:  # type: ignore[no-untyped-def]
            kwargs.setdefault("width", 200)
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(_RichConsole, "__init__", wide_init)

        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        # Pull the row's full fingerprint hex from JSON-mode, then
        # confirm its 16-char prefix appears in the human render.
        cli_main(["audit", "list", "--store-path", str(store_path), "--json"])
        json_out = capsys.readouterr().out.strip()
        full_hex = json.loads(json_out)["fingerprint"]
        cli_main(["audit", "list", "--store-path", str(store_path)])
        out = capsys.readouterr().out
        assert full_hex[:16] in out
        # Old header was "fingerprint (first 12)"; make sure the
        # widened display also dropped the parenthetical width hint.
        assert "(first 12)" not in out

    def test_json_mode_includes_pii_categories(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        cli_main(["audit", "list", "--store-path", str(store_path), "--json"])
        row = json.loads(capsys.readouterr().out.strip())
        assert "pii_categories" in row
        # Default seed has no PII; the field is the empty string,
        # not omitted, not None.
        assert row["pii_categories"] == ""

    def test_recent_timestamp_renders_short_form(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Rows just written are < 24h old; the renderer must compact
        # them to HH:MM:SS rather than the full microsecond ISO.
        store_path = tmp_path / "store.db"
        _seed_store_with_audit_rows(store_path, n=1)
        cli_main(["audit", "list", "--store-path", str(store_path)])
        out = capsys.readouterr().out
        # Pull the full timestamp from JSON-mode for comparison.
        cli_main(["audit", "list", "--store-path", str(store_path), "--json"])
        full_iso = json.loads(capsys.readouterr().out.strip())["occurred_at"]
        # The full ISO must NOT appear (we compacted it).
        assert full_iso not in out


class TestFormatAuditOccurredAt:
    """Direct coverage on the timestamp helper so age branches are exercised."""

    def test_recent_returns_hhmmss(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        iso = "2026-05-17T11:30:45.123456Z"  # 29 min 15s before `now`
        assert _format_audit_occurred_at(iso, now=now) == "11:30:45"

    def test_old_returns_full_iso(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        iso = "2026-05-15T11:30:45.123456Z"  # 2 days before `now`
        assert _format_audit_occurred_at(iso, now=now) == iso

    def test_future_timestamp_returns_full_iso(self) -> None:
        # Future timestamps (clock skew, test seeds) MUST NOT compact;
        # a bare HH:MM:SS with no date would mislead the operator into
        # reading a future row as today's.
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        iso = "2026-05-17T14:00:00.000000Z"  # 2 hours in the future
        assert _format_audit_occurred_at(iso, now=now) == iso

    def test_malformed_iso_returns_raw(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        bogus = "not-a-timestamp"
        assert _format_audit_occurred_at(bogus, now=now) == bogus
