"""Tests for `AuditWriter` and `build_audit_row`.

Covers single-row writes (all 14 fields populated correctly), chain
advancement across multiple writes, concurrent-write serialisation via
the lock, idempotent close, and `_load_tail_chain_hash` recovery on a
re-open. Also pins the v1-constant fields (`cost_class="small"`,
`pii_categories=""`, refusal / AST / rule = None) per ADR 0001.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from schemabrain.audit.chain import GENESIS_CHAIN_HASH
from schemabrain.audit.fingerprint import FINGERPRINT_VERSION
from schemabrain.audit.writer import AuditWriter, build_audit_row


class _FakeResponse:
    def __init__(self, status: str = "success") -> None:
        self.status = status


class TestBuildAuditRow:
    def test_minimal_response_populates_v1_defaults(self) -> None:
        draft = build_audit_row(
            tool_name="describe_table",
            source_connection_id="src1",
            response=_FakeResponse(status="success"),
        )
        assert draft == {
            "source_connection_id": "src1",
            "caller_id": None,
            "tool_name": "describe_table",
            "status": "success",
            "refusal_reason": None,
            "cost_class": "small",
            "pii_categories": frozenset(),
            "ast_shape_hash": None,
            "rule_id": None,
            "fingerprint_version": FINGERPRINT_VERSION,
        }

    def test_unknown_status_falls_back_to_error(self) -> None:
        """A response with a status outside the SQL CHECK enum would
        fail the INSERT. Falling back keeps the row writable so the
        audit chain doesn't gap on a programming bug."""
        draft = build_audit_row(
            tool_name="describe_table",
            source_connection_id="src1",
            response=_FakeResponse(status="wat"),
        )
        assert draft["status"] == "error"

    def test_missing_status_attribute_falls_back_to_error(self) -> None:
        """A response object with no `.status` attribute at all (a
        wrapper bug) also falls back."""

        class _Bare:
            pass

        draft = build_audit_row(
            tool_name="describe_table",
            source_connection_id="src1",
            response=_Bare(),
        )
        assert draft["status"] == "error"

    def test_each_charter_status_passes_through(self) -> None:
        for status in ("success", "empty", "partial", "degraded", "error", "refused"):
            draft = build_audit_row(
                tool_name="describe_table",
                source_connection_id="src1",
                response=_FakeResponse(status=status),
            )
            assert draft["status"] == status


def _draft(**overrides: object) -> dict[str, object]:
    base = build_audit_row(
        tool_name="describe_table",
        source_connection_id="src1",
        response=_FakeResponse(status="success"),
    )
    base.update(overrides)
    return base


class TestAuditWriterSingleWrite:
    def test_first_row_uses_genesis_prev_chain(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "store.db")
        try:
            row = writer.write(_draft())
            assert row.id == 1
            assert row.tool_name == "describe_table"
            assert row.status == "success"
            assert row.source_connection_id == "src1"
            assert row.caller_id is None
            assert row.refusal_reason is None
            assert row.cost_class == "small"
            assert row.pii_categories == ""
            assert row.ast_shape_hash is None
            assert row.rule_id is None
            assert row.fingerprint_version == FINGERPRINT_VERSION
            assert len(row.fingerprint) == 32
            assert len(row.chain_hash) == 32
            # First row's chain hash depends on GENESIS_CHAIN_HASH.
            # Distinct from the genesis itself.
            assert row.chain_hash != GENESIS_CHAIN_HASH
        finally:
            writer.close()

    def test_fingerprint_hex_property(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "store.db")
        try:
            row = writer.write(_draft())
            assert row.fingerprint_hex == row.fingerprint.hex()
            assert len(row.fingerprint_hex) == 64
        finally:
            writer.close()

    def test_persists_to_disk(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        writer = AuditWriter(db_path)
        try:
            writer.write(_draft())
        finally:
            writer.close()
        # Re-open with a fresh connection and read back.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM mcp_audit").fetchall()
            assert len(rows) == 1
            assert rows[0]["tool_name"] == "describe_table"
            assert rows[0]["status"] == "success"
        finally:
            conn.close()


class TestAuditWriterChainAdvancement:
    def test_three_row_sequence_chain_advances(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "store.db")
        try:
            row1 = writer.write(_draft(tool_name="describe_table"))
            row2 = writer.write(_draft(tool_name="describe_column"))
            row3 = writer.write(_draft(tool_name="describe_table"))
            assert row1.id == 1
            assert row2.id == 2
            assert row3.id == 3
            assert row1.chain_hash != row2.chain_hash
            assert row2.chain_hash != row3.chain_hash
            # row1 and row3 share the same tool_name + same v1 defaults
            # but advance the chain via id + occurred_at differing.
            assert row1.chain_hash != row3.chain_hash
        finally:
            writer.close()

    def test_v1_rows_share_identical_fingerprint(self, tmp_path: Path) -> None:
        """All v1 audit rows have the same fingerprint by design (only
        cost_class / PII / AST / refusal_reason differentiate, and v1
        ships constants for all four). Chain hash still differs because
        id + occurred_at are in the canonical form."""
        writer = AuditWriter(tmp_path / "store.db")
        try:
            row1 = writer.write(_draft(tool_name="describe_table"))
            row2 = writer.write(_draft(tool_name="get_metric"))
            assert row1.fingerprint == row2.fingerprint
            assert row1.chain_hash != row2.chain_hash
        finally:
            writer.close()


class TestAuditWriterReopen:
    def test_load_tail_chain_hash_recovers_on_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        writer1 = AuditWriter(db_path)
        try:
            row1 = writer1.write(_draft(tool_name="describe_table"))
        finally:
            writer1.close()
        # Re-open; the chain should pick up from row1.chain_hash, not
        # restart from GENESIS_CHAIN_HASH.
        writer2 = AuditWriter(db_path)
        try:
            row2 = writer2.write(_draft(tool_name="describe_column"))
            # row2.chain_hash == sha256(row1.chain_hash || canonical(row2))
            assert row2.id == 2
            # Distinct from "chain restarted at genesis" — which would
            # produce sha256(GENESIS || canonical(row2)).
            from schemabrain.audit.canonical import canonical_audit_row
            from schemabrain.audit.chain import compute_chain_hash

            canonical2 = canonical_audit_row(
                {
                    "id": row2.id,
                    "occurred_at": row2.occurred_at,
                    "source_connection_id": row2.source_connection_id,
                    "caller_id": row2.caller_id,
                    "tool_name": row2.tool_name,
                    "status": row2.status,
                    "refusal_reason": row2.refusal_reason,
                    "cost_class": row2.cost_class,
                    "pii_categories": row2.pii_categories,
                    "ast_shape_hash": row2.ast_shape_hash,
                    "rule_id": row2.rule_id,
                    "fingerprint": row2.fingerprint,
                    "fingerprint_version": row2.fingerprint_version,
                }
            )
            expected_from_row1 = compute_chain_hash(row1.chain_hash, canonical2)
            expected_from_genesis = compute_chain_hash(GENESIS_CHAIN_HASH, canonical2)
            assert row2.chain_hash == expected_from_row1
            assert row2.chain_hash != expected_from_genesis
        finally:
            writer2.close()

    def test_empty_table_starts_at_genesis(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "store.db")
        try:
            assert writer._last_chain_hash == GENESIS_CHAIN_HASH
        finally:
            writer.close()


class TestAuditWriterClose:
    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "store.db")
        writer.close()
        writer.close()  # must not raise

    def test_write_after_close_raises(self, tmp_path: Path) -> None:
        writer = AuditWriter(tmp_path / "store.db")
        writer.close()
        with pytest.raises(RuntimeError, match="closed"):
            writer.write(_draft())


class TestAuditWriterConcurrency:
    def test_two_threads_writing_serialise(self, tmp_path: Path) -> None:
        """Two threads writing concurrently must both succeed; the
        lock + per-write `SELECT max(id)` + INSERT pattern serialises
        id assignment so neither thread sees a UNIQUE constraint
        violation."""
        writer = AuditWriter(tmp_path / "store.db")
        results: list[int] = []
        errors: list[BaseException] = []
        n_per_thread = 20

        def _worker() -> None:
            try:
                for _ in range(n_per_thread):
                    row = writer.write(_draft())
                    results.append(row.id)
            except BaseException as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        try:
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            writer.close()

        assert errors == []
        assert len(results) == 2 * n_per_thread
        # All ids unique + contiguous.
        assert sorted(results) == list(range(1, 2 * n_per_thread + 1))


class TestAuditWriterCorruptedTail:
    """A corrupted last-row `chain_hash` (wrong byte width) used to be
    silently propagated — every subsequent write then failed with
    'prev must be 32 bytes' caught by the BUG path. The fix raises
    at construction time so the operator sees a clear message and the
    serve fallback path catches it cleanly."""

    def test_short_chain_hash_raises_at_load(self, tmp_path: Path) -> None:
        import sqlite3 as _sqlite3

        db_path = tmp_path / "store.db"
        writer = AuditWriter(db_path)
        try:
            writer.write(_draft())
        finally:
            writer.close()

        # Tamper the chain_hash to be too short. The trigger blocks
        # UPDATE so drop it temporarily.
        conn = _sqlite3.connect(db_path)
        try:
            conn.execute("DROP TRIGGER mcp_audit_no_update")
            conn.execute(
                "UPDATE mcp_audit SET chain_hash = ? WHERE id = 1",
                (b"\x00" * 16,),
            )
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(ValueError, match="corrupted chain_hash"):
            AuditWriter(db_path)


class TestBrokenResponseWarning:
    def test_warning_emitted_once_per_tool_type_pair(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated broken-response calls log once per (tool, status-class)
        pair so a recurring bug stays visible without flooding stderr."""

        from schemabrain.audit import writer as writer_mod

        monkeypatch.setattr(writer_mod, "_broken_response_logged", set())

        # First call — should log.
        writer_mod._warn_broken_response("describe_table", None)
        # Second call with same args — should be deduped.
        writer_mod._warn_broken_response("describe_table", None)
        # Third call with different tool — should log again.
        writer_mod._warn_broken_response("describe_column", None)

        err = capsys.readouterr().err
        # Two WARNING lines: one per (tool, class) pair.
        assert err.count("WARNING") == 2
        assert "describe_table" in err
        assert "describe_column" in err


class TestAuditWriterInitDir:
    def test_creates_parent_directory_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "dir" / "store.db"
        writer = AuditWriter(nested)
        try:
            row = writer.write(_draft())
            assert row.id == 1
            assert nested.exists()
        finally:
            writer.close()

    def test_memory_path_skips_dir_creation_and_pragmas(self) -> None:
        """`:memory:` SQLite has no file and no WAL-meaningful sync —
        the init code path branches on the path string to skip both."""
        writer = AuditWriter(":memory:")
        try:
            row = writer.write(_draft())
            assert row.id == 1
        finally:
            writer.close()
