"""Tests for `schemabrain.audit.verify.walk_chain`.

Pins the chain-verification behaviour: clean chains yield nothing;
content tampering yields one mismatch; chain-hash tampering yields
cascading mismatches; the default first-mismatch-stop semantics; the
`full` flag that walks past mismatches.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from schemabrain.audit.verify import ChainMismatch, walk_chain
from schemabrain.audit.writer import AuditWriter, build_audit_row


class _FakeResponse:
    def __init__(self, status: str = "success") -> None:
        self.status = status


def _draft(**overrides: object) -> dict[str, object]:
    base = build_audit_row(
        tool_name="describe_table",
        source_connection_id="src1",
        response=_FakeResponse(status="success"),
    )
    base.update(overrides)
    return base


def _seed(writer: AuditWriter, n: int = 3) -> None:
    for _ in range(n):
        writer.write(_draft())


def _writer_conn(writer: AuditWriter) -> sqlite3.Connection:
    return writer._require_conn()


class TestWalkChainClean:
    def test_empty_table_yields_no_mismatches(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        try:
            mismatches = list(walk_chain(_writer_conn(writer)))
            assert mismatches == []
        finally:
            writer.close()

    def test_clean_chain_yields_no_mismatches(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "s.db")
        try:
            _seed(writer, n=5)
            mismatches = list(walk_chain(_writer_conn(writer)))
            assert mismatches == []
        finally:
            writer.close()


class TestWalkChainTampering:
    def test_content_tampering_yields_one_mismatch(self, tmp_path: Path) -> None:
        """Tampering with row 2's content (without rewriting its
        chain_hash) makes row 2 fail. Row 3 still verifies because
        the writer used row 2's STORED chain_hash to compute row 3's
        chain — and that stored value is unchanged."""
        db_path = tmp_path / "s.db"
        writer = AuditWriter(db_path)
        try:
            _seed(writer, n=3)
        finally:
            writer.close()

        # Tamper row 2's tool_name directly. The trigger blocks UPDATE
        # against `mcp_audit`, so for this test we drop the trigger
        # temporarily — represents an attacker who has bypassed the
        # trigger via file-level write.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute("UPDATE mcp_audit SET tool_name = 'tampered' WHERE id = 2")
            conn.commit()
            mismatches = list(walk_chain(conn, full=True))
        finally:
            conn.close()

        # Row 2 fails (content changed without chain rewrite).
        # Rows 1 and 3 verify cleanly.
        assert [m.row_id for m in mismatches] == [2]

    def test_chain_hash_tampering_cascades(self, tmp_path: Path) -> None:
        """Tampering with row 2's chain_hash makes row 2 fail. Row 3's
        chain was computed using the OLD chain_2 but the verifier
        uses the new (tampered) chain_2 as prev for row 3 — mismatch
        cascades."""
        db_path = tmp_path / "s.db"
        writer = AuditWriter(db_path)
        try:
            _seed(writer, n=3)
        finally:
            writer.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute(
                "UPDATE mcp_audit SET chain_hash = ? WHERE id = 2",
                (b"\xff" * 32,),
            )
            conn.commit()
            mismatches = list(walk_chain(conn, full=True))
        finally:
            conn.close()

        # Row 2 fails (chain_hash doesn't match recomputed value).
        # Row 3 fails (prev_chain for row 3 is now the tampered chain).
        assert [m.row_id for m in mismatches] == [2, 3]

    def test_full_false_stops_at_first_mismatch(self, tmp_path: Path) -> None:
        db_path = tmp_path / "s.db"
        writer = AuditWriter(db_path)
        try:
            _seed(writer, n=5)
        finally:
            writer.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            # Tamper rows 2 AND 4.
            conn.execute(
                "UPDATE mcp_audit SET chain_hash = ? WHERE id = 2",
                (b"\xff" * 32,),
            )
            conn.execute(
                "UPDATE mcp_audit SET chain_hash = ? WHERE id = 4",
                (b"\xee" * 32,),
            )
            conn.commit()

            # full=False stops at first mismatch (row 2).
            mismatches = list(walk_chain(conn, full=False))
            assert len(mismatches) == 1
            assert mismatches[0].row_id == 2
        finally:
            conn.close()

    def test_full_true_walks_past_mismatches(self, tmp_path: Path) -> None:
        db_path = tmp_path / "s.db"
        writer = AuditWriter(db_path)
        try:
            _seed(writer, n=5)
        finally:
            writer.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute("UPDATE mcp_audit SET tool_name = 'tampered' WHERE id = 2")
            conn.execute("UPDATE mcp_audit SET tool_name = 'tampered' WHERE id = 4")
            conn.commit()

            mismatches = list(walk_chain(conn, full=True))
            ids = [m.row_id for m in mismatches]
            # Content-tampering on rows 2 and 4 produces independent
            # failures (no cascade because their chain_hashes are
            # unchanged — only the canonical bytes used to recompute
            # the expected value).
            assert 2 in ids
            assert 4 in ids
        finally:
            conn.close()


class TestChainMismatchShape:
    def test_mismatch_carries_expected_and_actual_hex(self, tmp_path: Path) -> None:
        db_path = tmp_path / "s.db"
        writer = AuditWriter(db_path)
        try:
            _seed(writer, n=1)
        finally:
            writer.close()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute(
                "UPDATE mcp_audit SET chain_hash = ? WHERE id = 1",
                (b"\x00" * 32,),
            )
            conn.commit()
            mismatches = list(walk_chain(conn))
            assert len(mismatches) == 1
            m = mismatches[0]
            assert isinstance(m, ChainMismatch)
            assert m.row_id == 1
            assert m.actual_hex == "00" * 32
            assert len(m.expected_hex) == 64
            assert m.expected_hex != m.actual_hex
        finally:
            conn.close()
