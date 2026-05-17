"""DDL for the `mcp_audit` append-only table.

Mirrors ADR 0001 section 1 (the 14-field table) and section 2
(append-only invariants via SQL triggers). The schema-version bump is
owned by `schemabrain/core/store.py:_SCHEMA_VERSION`; this module is
called from `_init_schema` so the version + DDL live in lock-step.

Three mechanisms enforce append-only per ADR 0001:
  - SQL triggers (this file) reject UPDATE and DELETE
  - Writer code-path discipline (`schemabrain/audit/writer.py` opens
    its own connection and exposes only INSERT)
  - The per-row chain hash (`chain.py`) makes coherent tampering
    detectable from any external archive
"""

from __future__ import annotations

import sqlite3

_DDL_STATEMENTS: tuple[str, ...] = (
    # Field-by-field comments live in the ADR; the SQL stays compact
    # for readability. Status, refusal_reason, and cost_class CHECK
    # constraints mirror the Charter envelope + ADR enums so a row
    # that wouldn't survive Python validation also wouldn't survive
    # SQL insertion.
    # `id INTEGER PRIMARY KEY` is a rowid alias — SQLite assigns
    # monotonically-increasing values. AUTOINCREMENT would add a
    # `sqlite_sequence` table for strict no-reuse semantics, but the
    # append-only triggers forbid DELETE anyway, so rowid reuse can
    # never occur. AUTOINCREMENT's extra per-INSERT UPDATE on
    # `sqlite_sequence` would be pure overhead.
    """
    CREATE TABLE IF NOT EXISTS mcp_audit (
        id                   INTEGER PRIMARY KEY,
        occurred_at          TEXT    NOT NULL,
        source_connection_id TEXT    NOT NULL,
        caller_id            TEXT,
        tool_name            TEXT    NOT NULL,
        status               TEXT    NOT NULL
            CHECK (status IN ('success','empty','partial','degraded','error','refused')),
        refusal_reason       TEXT
            CHECK (refusal_reason IS NULL OR refusal_reason IN (
                'pii_blocked',
                'allowlist_violation',
                'fragment_unsafe',
                'cost_cap_exceeded',
                'ambiguous_resolution',
                'schema_drift'
            )),
        cost_class           TEXT    NOT NULL
            CHECK (cost_class IN ('small','medium','large','refused')),
        pii_categories       TEXT    NOT NULL DEFAULT '',
        ast_shape_hash       BLOB,
        rule_id              TEXT,
        fingerprint          BLOB    NOT NULL,
        fingerprint_version  TEXT    NOT NULL,
        chain_hash           BLOB    NOT NULL
    )
    """,
    # Append-only triggers. The message body is matched by the
    # writer-side test suite (`match="append-only"`); changing the
    # wording is a coordinated breaking change.
    """
    CREATE TRIGGER IF NOT EXISTS mcp_audit_no_update
    BEFORE UPDATE ON mcp_audit
    BEGIN
        SELECT RAISE(ABORT, 'mcp_audit is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS mcp_audit_no_delete
    BEFORE DELETE ON mcp_audit
    BEGIN
        SELECT RAISE(ABORT, 'mcp_audit is append-only');
    END
    """,
    # Indexes for the two predicates `audit list` and any future
    # fleet-aggregation query will hit. `occurred_at` for time-range
    # filtering; `fingerprint` for grouping rows by refusal pattern.
    """
    CREATE INDEX IF NOT EXISTS idx_mcp_audit_occurred_at
        ON mcp_audit (occurred_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_mcp_audit_fingerprint
        ON mcp_audit (fingerprint)
    """,
)


def ensure_audit_schema(conn: sqlite3.Connection) -> None:
    """Create the `mcp_audit` table + triggers + indexes idempotently.

    Safe to call against an existing connection on every store open;
    `CREATE ... IF NOT EXISTS` makes each statement a no-op when the
    object already exists. The caller is responsible for the
    transaction boundary — wrap in `with conn:` for atomicity. This
    function deliberately does NOT open its own transaction so it can
    participate in a larger atomic block (e.g. core DDL + version
    stamp + audit DDL in `SQLiteStore._init_schema`).
    """
    for stmt in _DDL_STATEMENTS:
        conn.execute(stmt)
