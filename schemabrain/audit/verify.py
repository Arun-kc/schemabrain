"""Chain-walker for `schemabrain audit verify`.

Walks `mcp_audit` rows in id order, recomputing each row's
`chain_hash` from the previous STORED chain hash plus the row's
canonical bytes. Yields a `ChainMismatch` for every row where the
recomputed value diverges from the stored value.

By default the walk stops at the first mismatch — most operator-day
scenarios just need a yes/no "is the chain intact?" answer. Forensic
walks pass `full=True` to collect every mismatch.

Cascade behaviour: when a tampered row breaks the chain, subsequent
rows that were written using the now-stale chain still verify cleanly
against their own stored prev (because the writer used the stored
value and the verifier reads the same stored value). When a chain_hash
itself is tampered, subsequent rows fail too. Both cases are
correctly reported.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from schemabrain.audit.canonical import canonical_audit_row
from schemabrain.audit.chain import GENESIS_CHAIN_HASH, compute_chain_hash

# The 13 content fields the canonical form expects. Spelled out
# explicitly so the SELECT column list and the dict assembly stay in
# lock-step with `canonical.AUDIT_ROW_FIELDS`.
_ROW_COLUMNS = (
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
)


@dataclass(frozen=True, slots=True)
class ChainMismatch:
    """One detected mismatch between the stored and recomputed chain
    hash for a single audit row.
    """

    row_id: int
    expected_hex: str
    actual_hex: str


def walk_chain(conn: sqlite3.Connection, *, full: bool = False) -> Iterator[ChainMismatch]:
    """Walk `mcp_audit` rows; yield one `ChainMismatch` per mismatch.

    With `full=False` (default) stops after the first mismatch — the
    common "is the chain intact?" check exits early. With `full=True`
    continues past mismatches so forensic walks report every break.
    """
    prev_chain = GENESIS_CHAIN_HASH
    select_cols = ", ".join(_ROW_COLUMNS) + ", chain_hash"
    cursor = conn.execute(f"SELECT {select_cols} FROM mcp_audit ORDER BY id ASC")
    for row in cursor:
        actual_chain = bytes(row["chain_hash"])
        canonical_dict = _row_to_canonical_dict(row)
        canonical = canonical_audit_row(canonical_dict)
        expected_chain = compute_chain_hash(prev_chain, canonical)
        if expected_chain != actual_chain:
            yield ChainMismatch(
                row_id=row["id"],
                expected_hex=expected_chain.hex(),
                actual_hex=actual_chain.hex(),
            )
            if not full:
                return
        # Advance with the stored value — matches what the writer
        # would have used for the next row.
        prev_chain = actual_chain


def _row_to_canonical_dict(row: sqlite3.Row) -> dict[str, object]:
    """Convert a SQLite row to the dict shape `canonical_audit_row`
    expects. BLOB fields normalise to `bytes` so the canonical
    serialiser sees consistent types regardless of the sqlite3 binding.
    """
    out: dict[str, object] = {}
    for col in _ROW_COLUMNS:
        value = row[col]
        if col in ("ast_shape_hash", "fingerprint") and value is not None:
            value = bytes(value)
        out[col] = value
    return out
