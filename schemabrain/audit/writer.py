"""`AuditWriter` — the only writer of `mcp_audit` rows.

Opens its own sqlite3 connection (separate from `SQLiteStore`'s) so the
write-only contract is enforced by code-path discipline rather than by
SQL roles SQLite doesn't have. Same database file. Concurrent writes
from multiple threads serialise through `self._lock`; the single
writer connection avoids cross-connection lock contention SQLite
otherwise raises busy-timeout errors for.

`build_audit_row` is a pure helper that converts a per-call context
(tool name, source connection id, the tool's response) into a draft
dict. The writer fills in the timestamp, the fingerprint, the chain
hash, and the auto-assigned id, then INSERTs.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from schemabrain.audit.canonical import canonical_audit_row
from schemabrain.audit.chain import GENESIS_CHAIN_HASH, compute_chain_hash
from schemabrain.audit.ddl import ensure_audit_schema
from schemabrain.audit.fingerprint import (
    FINGERPRINT_VERSION,
    FingerprintInput,
    compute_fingerprint,
)

# Mirrors the SQL CHECK on the `status` column. Used to fall back to
# `"error"` when a tool returns a response without a recognisable
# status — keeps the row writable rather than failing the INSERT under
# adversarial / programming-bug inputs.
_ALLOWED_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "empty", "partial", "degraded", "error", "refused"}
)


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One persisted row, returned by `AuditWriter.write`.

    Callers (the `@instrument` decorator) use the `fingerprint` field
    to inject a real hex digest into response envelopes that carry a
    `fingerprint` field of their own (today: `MetricResult`).
    """

    id: int
    occurred_at: str
    source_connection_id: str
    caller_id: str | None
    tool_name: str
    status: str
    refusal_reason: str | None
    cost_class: str
    pii_categories: str
    ast_shape_hash: bytes | None
    rule_id: str | None
    fingerprint: bytes
    fingerprint_version: str
    chain_hash: bytes

    @property
    def fingerprint_hex(self) -> str:
        return self.fingerprint.hex()


def build_audit_row(
    *,
    tool_name: str,
    source_connection_id: str,
    response: Any,
) -> dict[str, Any]:
    """Build the per-call portion of an audit row.

    The writer fills in `id`, `occurred_at`, `fingerprint`, and
    `chain_hash`. The remaining fields come from this function:
    `tool_name` + `source_connection_id` from the decorator's closure,
    `status` from the response envelope, and v1 constants for the
    refusal / PII / AST / rule fields (no v1 tool populates these;
    they're nullable in the schema until v2 brings real values).
    """
    raw_status = getattr(response, "status", None)
    status = raw_status if raw_status in _ALLOWED_STATUSES else "error"
    return {
        "source_connection_id": source_connection_id,
        "caller_id": None,
        "tool_name": tool_name,
        "status": status,
        "refusal_reason": None,
        "cost_class": "small",
        "pii_categories": "",
        "ast_shape_hash": None,
        "rule_id": None,
        "fingerprint_version": FINGERPRINT_VERSION,
    }


def _now_iso_utc() -> str:
    """ISO 8601 UTC timestamp with microsecond precision and trailing Z.

    Duplicated from `schemabrain.observability.instrument.now_iso_utc`
    to avoid a circular import between the audit + observability
    packages — both are small and the format is a fixed contract.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


class AuditWriter:
    """Persist one row per MCP tool call to the `mcp_audit` table.

    Construction opens a dedicated sqlite3 connection against the
    store's database file, applies the audit-schema DDL (idempotent),
    and reads the most recent row's `chain_hash` so subsequent writes
    extend the existing chain.

    `write(draft)` is the only mutating entry point. Concurrent writes
    serialise through `self._lock`; the lock cost is negligible
    against the SQLite syscall cost.
    """

    def __init__(self, db_path: Path | str) -> None:
        path_str = str(db_path)
        if path_str != ":memory:":
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(path_str, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + NORMAL mirror the SQLiteStore choice; concurrent
        # readers (audit verify / audit list) and the writer coexist.
        if path_str != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        ensure_audit_schema(self._conn)
        self._lock = threading.Lock()
        self._last_chain_hash = self._load_tail_chain_hash()

    def _load_tail_chain_hash(self) -> bytes:
        """Read the most recent row's `chain_hash` so the next write
        extends the chain rather than restarting from genesis.

        Returns `GENESIS_CHAIN_HASH` when the table is empty.
        """
        conn = self._require_conn()
        row = conn.execute("SELECT chain_hash FROM mcp_audit ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return GENESIS_CHAIN_HASH
        # SQLite returns BLOB as memoryview in some bindings; bytes()
        # normalises.
        return bytes(row["chain_hash"])

    def write(self, draft: dict[str, Any]) -> AuditRow:
        """Persist one row and return the materialised `AuditRow`.

        The draft (from `build_audit_row`) provides the per-call fields;
        the writer fills in `id`, `occurred_at`, `fingerprint`, and
        `chain_hash`. The transaction commits before this method
        returns — a caller seeing the returned `AuditRow` knows the
        row is durable (modulo the WAL + NORMAL sync policy).
        """
        with self._lock:
            conn = self._require_conn()
            occurred_at = _now_iso_utc()

            # Compute fingerprint over the v1 input set. The PII /
            # AST / refusal fields are all v1-constant; v2 widens
            # them through `build_audit_row` rather than touching
            # the writer.
            fp_input = FingerprintInput(
                ast_shape_hash=draft["ast_shape_hash"],
                pii_tags_touched=frozenset(),
                refusal_reason=draft["refusal_reason"],
                cost_class=draft["cost_class"],
                rule_id=draft["rule_id"],
            )
            fingerprint = compute_fingerprint(fp_input)

            # Determine next id under the lock so the canonical form
            # (which includes id) is computable before the INSERT.
            max_id_row = conn.execute("SELECT max(id) AS m FROM mcp_audit").fetchone()
            next_id = (max_id_row["m"] or 0) + 1

            canonical_dict: dict[str, Any] = {
                "id": next_id,
                "occurred_at": occurred_at,
                "source_connection_id": draft["source_connection_id"],
                "caller_id": draft["caller_id"],
                "tool_name": draft["tool_name"],
                "status": draft["status"],
                "refusal_reason": draft["refusal_reason"],
                "cost_class": draft["cost_class"],
                "pii_categories": draft["pii_categories"],
                "ast_shape_hash": draft["ast_shape_hash"],
                "rule_id": draft["rule_id"],
                "fingerprint": fingerprint,
                "fingerprint_version": draft["fingerprint_version"],
            }
            canonical = canonical_audit_row(canonical_dict)
            chain_hash = compute_chain_hash(self._last_chain_hash, canonical)

            with conn:
                conn.execute(
                    """
                    INSERT INTO mcp_audit (
                        id, occurred_at, source_connection_id, caller_id,
                        tool_name, status, refusal_reason, cost_class,
                        pii_categories, ast_shape_hash, rule_id,
                        fingerprint, fingerprint_version, chain_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_id,
                        occurred_at,
                        draft["source_connection_id"],
                        draft["caller_id"],
                        draft["tool_name"],
                        draft["status"],
                        draft["refusal_reason"],
                        draft["cost_class"],
                        draft["pii_categories"],
                        draft["ast_shape_hash"],
                        draft["rule_id"],
                        fingerprint,
                        draft["fingerprint_version"],
                        chain_hash,
                    ),
                )

            # Commit succeeded — advance the in-memory tail.
            self._last_chain_hash = chain_hash
            return AuditRow(
                id=next_id,
                occurred_at=occurred_at,
                source_connection_id=draft["source_connection_id"],
                caller_id=draft["caller_id"],
                tool_name=draft["tool_name"],
                status=draft["status"],
                refusal_reason=draft["refusal_reason"],
                cost_class=draft["cost_class"],
                pii_categories=draft["pii_categories"],
                ast_shape_hash=draft["ast_shape_hash"],
                rule_id=draft["rule_id"],
                fingerprint=fingerprint,
                fingerprint_version=draft["fingerprint_version"],
                chain_hash=chain_hash,
            )

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("AuditWriter is closed")
        return self._conn

    def close(self) -> None:
        """Idempotent close — safe to call multiple times."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
