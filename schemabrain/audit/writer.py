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
from typing import Any, Final, Literal

from schemabrain.audit.canonical import canonical_audit_row
from schemabrain.audit.chain import CHAIN_HASH_SIZE, GENESIS_CHAIN_HASH, compute_chain_hash
from schemabrain.audit.ddl import ensure_audit_schema
from schemabrain.audit.fingerprint import (
    FINGERPRINT_VERSION,
    CostClass,
    FingerprintInput,
    RefusalReason,
    compute_fingerprint,
)
from schemabrain.pii import PIICategory

# Mirrors the SQL CHECK on the `status` column AND the Charter envelope
# Status literal. Kept as a sibling Literal here so the audit module
# does not import from `schemabrain.mcp.envelope` (which would pull in
# pydantic and inflate the audit dependency graph).
AuditStatus = Literal["success", "empty", "partial", "degraded", "error", "refused"]

# Frozenset for the runtime fallback at `build_audit_row` time — when a
# tool returns a response with a status outside the enum, we coerce to
# "error" rather than fail the INSERT. The set is derived from the
# Literal so the two stay in lock-step.
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
    status: AuditStatus
    refusal_reason: RefusalReason | None
    cost_class: CostClass
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
    `status` from the response envelope, and PII categories pulled
    from either `response.data.pii_categories` (success path —
    today only `MetricResult` carries the field) or
    `response.error.pii_categories` (refusal path — populated by the
    `pii_blocked` refusal envelope).

    `pii_categories` is carried in the draft as
    `frozenset[PIICategory]` (the typed form). `AuditWriter.write`
    serialises it to the canonical comma-sorted CSV for the SQL
    column AND uses the typed value directly when constructing
    `FingerprintInput.pii_tags_touched` — no string round-trip,
    no `type: ignore` required.
    """
    raw_status = getattr(response, "status", None)
    if raw_status not in _ALLOWED_STATUSES:
        # A tool returning None or a bare object (forgot to wrap in
        # ToolResponse) hits this path. The audit row still lands as
        # `status="error"` so the chain stays gap-free, but the broken
        # tool surface needs to be visible to the contributor —
        # otherwise the bug hides behind a clean-looking error row.
        _warn_broken_response(tool_name, raw_status)
        status = "error"
    else:
        status = raw_status

    pii_categories = _extract_pii_categories(response)
    refusal_reason = _extract_refusal_reason(response, status)

    return {
        "source_connection_id": source_connection_id,
        "caller_id": None,
        "tool_name": tool_name,
        "status": status,
        "refusal_reason": refusal_reason,
        "cost_class": "refused" if status == "refused" else "small",
        "pii_categories": pii_categories,
        "ast_shape_hash": None,
        "rule_id": None,
        "fingerprint_version": FINGERPRINT_VERSION,
    }


def _extract_pii_categories(response: Any) -> frozenset[PIICategory]:
    """Read `pii_categories` from the response (success or refusal path).

    Success-side: `response.data.pii_categories` is the propagated
    set when present (today only `MetricResult` carries it).
    Refusal-side: `response.error.pii_categories` is the *blocked* set
    (policy intersection that triggered refusal) populated by the
    `pii_blocked` refusal envelope at `mcp/server.py`. The full attempted
    set is not currently persisted to the audit row — it lives only on
    `PiiBlockedError.attempted_categories`. Either side returns a sorted
    tuple of category strings (or any iterable of `PIICategory`-valued
    strings).

    Returns `frozenset()` when neither path produces a value — the
    schema column defaults to `''` for "no PII touched" via the
    CSV encoding step in `AuditWriter.write`.
    """
    data = getattr(response, "data", None)
    if data is not None:
        categories = getattr(data, "pii_categories", None)
        if categories:
            return frozenset(categories)
    error = getattr(response, "error", None)
    if error is not None:
        categories = getattr(error, "pii_categories", None)
        if categories:
            return frozenset(categories)
    return frozenset()


def _encode_pii_categories_csv(categories: frozenset[PIICategory]) -> str:
    """Serialise a typed category frozenset to the on-disk CSV form.

    Sorted so two writes carrying the same conceptual set produce the
    same bytes — matches the `example_queries.pii_categories`
    encoding convention and supports cross-table grep workflows.
    """
    if not categories:
        return ""
    return ",".join(sorted(categories))


def _extract_refusal_reason(response: Any, status: str) -> str | None:
    """Map a refused envelope's `error.kind` to the audit row's
    `refusal_reason`.

    Non-refused responses always carry `refusal_reason=None`.
    Refused responses today only emit `pii_blocked`; the other
    five refusal kinds in ADR 0001 (`allowlist_violation`,
    `fragment_unsafe`, `cost_cap_exceeded`,
    `ambiguous_resolution`, `schema_drift`) land in future PRs and
    extend the mapping below.
    """
    if status != "refused":
        return None
    error = getattr(response, "error", None)
    if error is None:
        return None
    kind = getattr(error, "kind", None)
    if kind == "pii_blocked":
        return "pii_blocked"
    return None


def _now_iso_utc() -> str:
    """ISO 8601 UTC timestamp with microsecond precision and trailing Z.

    Duplicated from `schemabrain.observability.instrument.now_iso_utc`
    to avoid a circular import between the audit + observability
    packages — both are small and the format is a fixed contract.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


_broken_response_logged: set[tuple[str, str]] = set()


def _warn_broken_response(tool_name: str, raw_status: Any) -> None:
    """One stderr line per (tool, status-class) pair. A repeating
    broken tool doesn't flood logs but stays visible the first time."""
    import sys

    key = (tool_name, type(raw_status).__name__)
    if key in _broken_response_logged:
        return
    _broken_response_logged.add(key)
    print(
        f"schemabrain audit WARNING: tool {tool_name!r} returned a "
        f"response with no .status (got {raw_status!r}). Writing "
        f"audit row with status='error'. Fix the tool to return a "
        f"ToolResponse-shaped object.",
        file=sys.stderr,
    )


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
        # ensure_audit_schema does NOT open its own transaction so it
        # can participate in larger atomic blocks elsewhere; here we
        # wrap it ourselves since this is a standalone construction.
        with self._conn:
            ensure_audit_schema(self._conn)
        self._lock = threading.Lock()
        self._last_chain_hash = self._load_tail_chain_hash()

    def _load_tail_chain_hash(self) -> bytes:
        """Read the most recent row's `chain_hash` so the next write
        extends the chain rather than restarting from genesis.

        Returns `GENESIS_CHAIN_HASH` when the table is empty. Raises
        `ValueError` if the stored chain_hash is not exactly 32 bytes
        (a corrupted DB) — the caller (`_cmd_serve`) catches and falls
        back to audit-disabled rather than silently producing rows
        every subsequent write would reject with "prev must be 32 bytes."
        """
        conn = self._require_conn()
        row = conn.execute(
            "SELECT id, chain_hash FROM mcp_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return GENESIS_CHAIN_HASH
        # SQLite returns BLOB as memoryview in some bindings; bytes()
        # normalises.
        raw = bytes(row["chain_hash"])
        if len(raw) != CHAIN_HASH_SIZE:
            raise ValueError(
                f"mcp_audit row {row['id']} has corrupted chain_hash "
                f"({len(raw)} bytes; expected {CHAIN_HASH_SIZE}). The "
                f"chain is unrecoverable from this row; restore from "
                f"backup or delete the store and re-index."
            )
        return raw

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

            # The draft carries `pii_categories` as the TYPED
            # `frozenset[PIICategory]` (set by `build_audit_row`).
            # FingerprintInput consumes the typed value directly;
            # the SQL column gets the CSV encoding alongside. No
            # round-trip through a comma-string, no type: ignore.
            pii_tags_touched: frozenset[PIICategory] = draft["pii_categories"]
            pii_categories_csv = _encode_pii_categories_csv(pii_tags_touched)

            # Compute fingerprint over the v1 input set. PII tags
            # touched now varies per call (the `get_metric`
            # propagation step populates it from the column tag
            # store); the AST shape and rule id are still v1-constant
            # pending v2 query-shape fingerprinting + rule resolution.
            fp_input = FingerprintInput(
                ast_shape_hash=draft["ast_shape_hash"],
                pii_tags_touched=pii_tags_touched,
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
                "pii_categories": pii_categories_csv,
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
                        pii_categories_csv,
                        draft["ast_shape_hash"],
                        draft["rule_id"],
                        fingerprint,
                        draft["fingerprint_version"],
                        chain_hash,
                    ),
                )

            # CRITICAL INVARIANT: only reached after `with conn:` exits
            # cleanly (commit succeeded). An exception inside the
            # `with` block re-raises here without executing this
            # assignment, leaving `_last_chain_hash` matching the
            # on-disk tail. Do NOT insert code between the `with`
            # block and this line — that would risk desyncing the
            # in-memory tail from the persisted state.
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
                pii_categories=pii_categories_csv,
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
