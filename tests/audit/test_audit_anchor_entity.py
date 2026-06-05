"""`mcp_audit.anchor_entity` — the non-canonical refusal-attribution column.

PR-17b adds a best-effort attribution of an error/refusal audit row to a
single entity, used by the graph surface's refusal-hotspot overlay. The
column is store-schema v17 and deliberately NON-CANONICAL: it is not a member
of `audit/canonical.py::AUDIT_ROW_FIELDS`, the writer never feeds it into
`canonical_audit_row`, and `verify._ROW_COLUMNS` / `_audit_leaf_preimages`
never select it. These tests pin the two load-bearing safety properties —

  1. the v16→v17 migration adds the column, preserves existing rows, and
     backfills NULL (and `ALTER ADD COLUMN` is not blocked by the append-only
     triggers), and
  2. the column never enters the hashed preimage: a row written WITH a
     populated `anchor_entity` still verifies cleanly (its stored `chain_hash`
     equals the recomputation that excludes the column), and the entity name
     is provably absent from the canonical leaf bytes —

plus the live per-entity aggregation `refusal_counts_by_entity` reads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from schemabrain.audit.canonical import canonical_audit_row
from schemabrain.audit.verify import _ROW_COLUMNS, _row_to_canonical_dict, walk_chain
from schemabrain.audit.writer import AuditWriter, build_audit_row
from schemabrain.core.store import SQLiteStore

SRC = "anchor-src"


# --- fakes mirroring the response surface `build_audit_row` reads -----------


class _FakeError:
    def __init__(self, *, anchor_entity: str | None, kind: str = "pii_blocked") -> None:
        self.kind = kind
        self.anchor_entity = anchor_entity
        # `pii_blocked` carries blocked categories; the writer reads them too.
        self.pii_categories = ("payment_card",) if kind == "pii_blocked" else ()


class _FakeResponse:
    def __init__(
        self,
        *,
        status: str = "refused",
        anchor_entity: str | None = None,
        kind: str = "pii_blocked",
    ) -> None:
        self.status = status
        self.data = None
        self.error = (
            _FakeError(anchor_entity=anchor_entity, kind=kind) if status != "success" else None
        )


def _write(writer: AuditWriter, **kwargs: Any) -> Any:
    draft = build_audit_row(
        tool_name="get_metric", source_connection_id=SRC, response=_FakeResponse(**kwargs)
    )
    return writer.write(draft)


# --- 1. migration safety ----------------------------------------------------


_V16_MCP_AUDIT_DDL = """
CREATE TABLE mcp_audit (
    id                   INTEGER PRIMARY KEY,
    occurred_at          TEXT    NOT NULL,
    source_connection_id TEXT    NOT NULL,
    caller_id            TEXT,
    tool_name            TEXT    NOT NULL,
    status               TEXT    NOT NULL
        CHECK (status IN ('success','empty','partial','degraded','error','refused')),
    refusal_reason       TEXT,
    cost_class           TEXT    NOT NULL
        CHECK (cost_class IN ('small','medium','large','refused')),
    pii_categories       TEXT    NOT NULL DEFAULT '',
    ast_shape_hash       BLOB,
    rule_id              TEXT,
    fingerprint          BLOB    NOT NULL,
    fingerprint_version  TEXT    NOT NULL,
    chain_hash           BLOB    NOT NULL
)
"""


def _build_v16_store(path: Path) -> None:
    """Hand-build a genuine pre-anchor_entity store: schema_version='16' and a
    14-column `mcp_audit` carrying one pre-existing chained row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schemabrain_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schemabrain_meta (key, value) VALUES ('schema_version', '16')")
        conn.execute(_V16_MCP_AUDIT_DDL)
        conn.execute(
            "INSERT INTO mcp_audit (id, occurred_at, source_connection_id, caller_id, "
            "tool_name, status, refusal_reason, cost_class, pii_categories, ast_shape_hash, "
            "rule_id, fingerprint, fingerprint_version, chain_hash) "
            "VALUES (1, '2026-01-01T00:00:00.000000Z', ?, NULL, 'get_metric', 'refused', "
            "'pii_blocked', 'refused', '', NULL, NULL, ?, 'fp-v1', ?)",
            (SRC, b"\xaa" * 32, b"\x00" * 32),
        )
        conn.commit()
    finally:
        conn.close()


def test_v16_to_v17_migration_adds_column_preserves_rows(tmp_path: Path) -> None:
    """Opening a v16 store migrates it to v17: the column appears, the
    pre-existing row survives, and its `anchor_entity` backfills to NULL
    (the append-only triggers do not block `ALTER TABLE ADD COLUMN`)."""
    path = tmp_path / "v16.db"
    _build_v16_store(path)

    with SQLiteStore(path):
        pass  # opening triggers the migration chain

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute(
            "SELECT value FROM schemabrain_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        assert version == "17"
        names = {r["name"] for r in conn.execute("PRAGMA table_info(mcp_audit)")}
        assert "anchor_entity" in names
        row = conn.execute("SELECT anchor_entity FROM mcp_audit WHERE id = 1").fetchone()
        assert row["anchor_entity"] is None  # backfilled, row preserved
    finally:
        conn.close()


def test_fresh_store_has_anchor_entity_column(tmp_path: Path) -> None:
    """A brand-new store gets the column straight from the audit DDL (no
    migration leg runs)."""
    path = tmp_path / "fresh.db"
    with SQLiteStore(path):
        pass
    conn = sqlite3.connect(path)
    try:
        names = {r[1] for r in conn.execute("PRAGMA table_info(mcp_audit)")}
        assert "anchor_entity" in names
    finally:
        conn.close()


def test_audit_writer_self_heals_a_pre_v17_mcp_audit(tmp_path: Path) -> None:
    """The AuditWriter is a SECOND writer of the store and does NOT run the
    SQLiteStore migration. Against a pre-v17 on-disk `mcp_audit` (table present
    WITHOUT `anchor_entity`), `ensure_audit_schema`'s `CREATE IF NOT EXISTS` is
    a no-op — so opening the writer must self-heal the column via an additive
    ALTER. Without it, the writer's INSERT (which enumerates `anchor_entity`)
    raises `OperationalError: table mcp_audit has no column named
    anchor_entity`. This pins the fix so the writer never depends on the
    SQLiteStore migration having committed first."""
    path = tmp_path / "pre17.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(_V16_MCP_AUDIT_DDL)  # 14-column table, no anchor_entity
        conn.commit()
    finally:
        conn.close()

    writer = AuditWriter(path)  # __init__ → ensure_audit_schema → self-heal
    try:
        row = _write(writer, status="refused", anchor_entity="order")
        assert row.anchor_entity == "order"  # write succeeds, no OperationalError
    finally:
        writer.close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        names = {r["name"] for r in conn.execute("PRAGMA table_info(mcp_audit)")}
        assert "anchor_entity" in names  # column was added by the self-heal
        stored = conn.execute(
            "SELECT anchor_entity FROM mcp_audit WHERE status = 'refused'"
        ).fetchone()
        assert stored["anchor_entity"] == "order"
    finally:
        conn.close()


# --- 2. canonical / chain safety -------------------------------------------


def test_writer_persists_anchor_entity_from_refusal(tmp_path: Path) -> None:
    writer = AuditWriter(tmp_path / "w.db")
    try:
        row = _write(writer, status="refused", anchor_entity="order")
        assert row.anchor_entity == "order"
    finally:
        writer.close()
    conn = sqlite3.connect(tmp_path / "w.db")
    conn.row_factory = sqlite3.Row
    try:
        stored = conn.execute("SELECT anchor_entity, status FROM mcp_audit WHERE id = 1").fetchone()
        assert stored["anchor_entity"] == "order"
        assert stored["status"] == "refused"
    finally:
        conn.close()


def test_success_response_has_null_anchor_entity(tmp_path: Path) -> None:
    writer = AuditWriter(tmp_path / "w.db")
    try:
        row = _write(writer, status="success")
        assert row.anchor_entity is None
    finally:
        writer.close()


def test_anchor_entity_does_not_enter_the_chain(tmp_path: Path) -> None:
    """The load-bearing safety property: a row written WITH a populated
    `anchor_entity` still verifies. `walk_chain` recomputes each row's
    `chain_hash` from the 13-field canonical preimage (which excludes
    `anchor_entity`); if the writer had folded the column into the preimage,
    the stored hash would diverge and `walk_chain` would report a mismatch."""
    path = tmp_path / "chain.db"
    writer = AuditWriter(path)
    try:
        _write(writer, status="refused", anchor_entity="order")
        _write(writer, status="success")
        _write(writer, status="refused", anchor_entity="payment_method")
    finally:
        writer.close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        mismatches = list(walk_chain(conn, full=True))
        assert mismatches == []  # chain intact despite populated anchors
    finally:
        conn.close()


def test_anchor_entity_name_absent_from_canonical_leaf(tmp_path: Path) -> None:
    """The entity name must never appear in the hashed leaf bytes — proves
    the Merkle leaf / chain preimage cannot leak (or be perturbed by) it."""
    path = tmp_path / "leaf.db"
    secret_name = "zzz_unique_entity_marker"
    writer = AuditWriter(path)
    try:
        _write(writer, status="refused", anchor_entity=secret_name)
    finally:
        writer.close()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        # Reconstruct the canonical leaf exactly as verify / merkle do.
        # `_ROW_COLUMNS` is a hardcoded module-level tuple — no user input.
        select_cols = ", ".join(_ROW_COLUMNS)
        sql = f"SELECT {select_cols} FROM mcp_audit WHERE id = 1"  # nosec B608
        row = conn.execute(sql).fetchone()
        leaf = canonical_audit_row(_row_to_canonical_dict(row))
    finally:
        conn.close()
    assert secret_name.encode() not in leaf


# --- 3. live aggregation ----------------------------------------------------


def test_refusal_counts_by_entity_aggregates_and_splits(tmp_path: Path) -> None:
    path = tmp_path / "agg.db"
    writer = AuditWriter(path)
    try:
        _write(writer, status="refused", anchor_entity="order")
        _write(writer, status="refused", anchor_entity="order")
        _write(writer, status="refused", anchor_entity="user")
        _write(writer, status="refused", anchor_entity=None)  # unattributed
        _write(writer, status="error", anchor_entity="order")  # not a refusal
        _write(writer, status="success")
    finally:
        writer.close()

    with SQLiteStore(path) as store:
        result = store.refusal_counts_by_entity(source_connection_id=SRC)
    assert result.by_entity == {"order": 2, "user": 1}  # error row excluded
    assert result.unattributed == 1


def test_refusal_counts_empty_without_refusals(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "empty.db") as store:
        result = store.refusal_counts_by_entity(source_connection_id=SRC)
    assert result.by_entity == {}
    assert result.unattributed == 0


def test_refusal_counts_scoped_to_source(tmp_path: Path) -> None:
    path = tmp_path / "scoped.db"
    writer = AuditWriter(path)
    try:
        draft = build_audit_row(
            tool_name="get_metric",
            source_connection_id="other-src",
            response=_FakeResponse(status="refused", anchor_entity="order"),
        )
        writer.write(draft)
    finally:
        writer.close()
    with SQLiteStore(path) as store:
        result = store.refusal_counts_by_entity(source_connection_id=SRC)
    assert result.by_entity == {}
    assert result.unattributed == 0
