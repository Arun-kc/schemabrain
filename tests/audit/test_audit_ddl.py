"""Tests for the `mcp_audit` DDL.

ADR 0001 mandates a 14-field table, two append-only triggers, and two
indexes (`occurred_at`, `fingerprint`). Tests pin each so a future
refactor that drops a trigger or renames a field fails loudly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from schemabrain.audit.ddl import ensure_audit_schema


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(tmp_path / "store.db")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestEnsureAuditSchema:
    def test_creates_table(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp_audit'"
        ).fetchone()
        assert row is not None

    def test_idempotent(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        ensure_audit_schema(conn)
        # Still one table, no exception.
        count = conn.execute(
            "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'mcp_audit'"
        ).fetchone()["n"]
        assert count == 1


class TestTableShape:
    def test_has_fifteen_columns(self, conn: sqlite3.Connection) -> None:
        # 14 ADR-0001 columns + the non-canonical `anchor_entity` (store v17).
        ensure_audit_schema(conn)
        cols = conn.execute("PRAGMA table_info(mcp_audit)").fetchall()
        assert len(cols) == 15

    def test_column_names_match_adr(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        names = {row["name"] for row in conn.execute("PRAGMA table_info(mcp_audit)")}
        assert names == {
            "id",
            "occurred_at",
            "source_connection_id",
            "caller_id",
            "tool_name",
            "status",
            "refusal_reason",
            "cost_class",
            "pii_categories",
            "ast_shape_hash",
            "rule_id",
            "fingerprint",
            "fingerprint_version",
            "chain_hash",
            # Non-canonical metadata (store v17) — NOT an AUDIT_ROW_FIELDS
            # member; never enters the chain hash. See `audit/ddl.py`.
            "anchor_entity",
        }

    def test_anchor_entity_is_not_a_canonical_field(self, conn: sqlite3.Connection) -> None:
        """The new column must NOT leak into the chained preimage: the
        canonical field set is unchanged at exactly the 13 ADR-0001 content
        fields, so every previously chained row keeps the identical leaf."""
        from schemabrain.audit.canonical import AUDIT_ROW_FIELDS

        assert "anchor_entity" not in AUDIT_ROW_FIELDS
        assert len(AUDIT_ROW_FIELDS) == 13

    def test_id_is_primary_key(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(mcp_audit)")}
        assert cols["id"]["pk"] == 1

    def test_status_check_constraint_rejects_unknown(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO mcp_audit "
                "(occurred_at, source_connection_id, tool_name, status, cost_class, "
                "fingerprint, fingerprint_version, chain_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-05-17T00:00:00Z",
                    "src1",
                    "find_relevant_tables",
                    "wat",  # not in the enum
                    "small",
                    b"\xaa" * 32,
                    "fp-v1",
                    b"\x00" * 32,
                ),
            )

    def test_refusal_reason_check_constraint_rejects_unknown(
        self, conn: sqlite3.Connection
    ) -> None:
        ensure_audit_schema(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO mcp_audit "
                "(occurred_at, source_connection_id, tool_name, status, refusal_reason, "
                "cost_class, fingerprint, fingerprint_version, chain_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-05-17T00:00:00Z",
                    "src1",
                    "describe_table",
                    "refused",
                    "wat",  # not in the six allowed
                    "refused",
                    b"\xaa" * 32,
                    "fp-v1",
                    b"\x00" * 32,
                ),
            )

    def test_cost_class_check_constraint_rejects_unknown(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO mcp_audit "
                "(occurred_at, source_connection_id, tool_name, status, cost_class, "
                "fingerprint, fingerprint_version, chain_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-05-17T00:00:00Z",
                    "src1",
                    "find_relevant_tables",
                    "success",
                    "huge",  # not in the four allowed
                    b"\xaa" * 32,
                    "fp-v1",
                    b"\x00" * 32,
                ),
            )


class TestAppendOnlyTriggers:
    def _insert_one(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        conn.execute(
            "INSERT INTO mcp_audit "
            "(occurred_at, source_connection_id, tool_name, status, cost_class, "
            "fingerprint, fingerprint_version, chain_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-05-17T00:00:00Z",
                "src1",
                "find_relevant_tables",
                "success",
                "small",
                b"\xaa" * 32,
                "fp-v1",
                b"\x00" * 32,
            ),
        )
        conn.commit()

    def test_update_rejected_by_trigger(self, conn: sqlite3.Connection) -> None:
        self._insert_one(conn)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE mcp_audit SET tool_name = 'wat' WHERE id = 1")

    def test_delete_rejected_by_trigger(self, conn: sqlite3.Connection) -> None:
        self._insert_one(conn)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM mcp_audit WHERE id = 1")

    def test_insert_allowed(self, conn: sqlite3.Connection) -> None:
        """The triggers MUST allow INSERT — they only block UPDATE +
        DELETE. Append-only ≠ no-writes."""
        self._insert_one(conn)
        # If we get here, INSERT worked.
        count = conn.execute("SELECT count(*) AS n FROM mcp_audit").fetchone()["n"]
        assert count == 1


class TestIndexes:
    def test_occurred_at_index_exists(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_mcp_audit_occurred_at'"
        ).fetchone()
        assert idx is not None

    def test_fingerprint_index_exists(self, conn: sqlite3.Connection) -> None:
        ensure_audit_schema(conn)
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_mcp_audit_fingerprint'"
        ).fetchone()
        assert idx is not None


class TestStoreIntegration:
    """After the version bump, SQLiteStore.__init__ must call
    ensure_audit_schema as part of its DDL pass. Verifying via the
    store rather than the bare connection catches a wiring regression
    if a future refactor pulls the call out."""

    def test_store_init_creates_audit_table(self, tmp_path: Path) -> None:
        from schemabrain.core.store import SQLiteStore

        store = SQLiteStore(tmp_path / "s.db")
        # Reach into the underlying connection by issuing a SELECT.
        rows = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mcp_audit'"
        ).fetchall()
        store.close()
        assert len(rows) == 1
