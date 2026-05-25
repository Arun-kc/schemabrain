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

Since-cursor walks (`audit verify --since <hash>` / `<duration>` /
`<iso>`) skip ahead to a chosen cursor row and use that row's STORED
`chain_hash` as the trusted baseline for verifying everything after
it. Use case: an operator who archived a known-good chain head
externally wants to confirm rows added since are intact, without
re-walking the (potentially huge) earlier history. Rows at or before
the cursor are NOT verified — by design — so tampering in that
segment will not be reported by a since-cursor walk.
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


def walk_chain(
    conn: sqlite3.Connection,
    *,
    full: bool = False,
    start_after_row_id: int | None = None,
) -> Iterator[ChainMismatch]:
    """Walk `mcp_audit` rows; yield one `ChainMismatch` per mismatch.

    With `full=False` (default) stops after the first mismatch — the
    common "is the chain intact?" check exits early. With `full=True`
    continues past mismatches so forensic walks report every break.

    With `start_after_row_id=None` (default) walks from genesis (row
    1's prev is `GENESIS_CHAIN_HASH`). With `start_after_row_id=N`
    walks only rows with `id > N`, using row N's STORED `chain_hash`
    as the trusted baseline for row N+1's verification. The cursor
    row's own integrity is NOT verified by this walk — it is treated
    as a trust anchor that the operator has separately archived. If
    no row with that id exists the walk yields nothing (no rows to
    verify), which the CLI surfaces as a zero-row result; deciding
    whether that is "intact" or "no-op" is the caller's concern.

    Rows whose canonical serialisation raises (corrupted shape, wrong
    binary widths, future-schema drift) surface as a synthetic
    `ChainMismatch` rather than crashing the iterator — a forensic
    walker should see every break, including unserialisable rows.
    """
    if start_after_row_id is None:
        prev_chain = GENESIS_CHAIN_HASH
    else:
        anchor = conn.execute(
            "SELECT chain_hash FROM mcp_audit WHERE id = ?", (start_after_row_id,)
        ).fetchone()
        if anchor is None:
            # Cursor row does not exist — no rows after it to verify
            # either. Caller surfaces this as the empty-walk case.
            return
        prev_chain = bytes(anchor["chain_hash"])
    # `_ROW_COLUMNS` is a module-level Final[tuple[str, ...]] of hardcoded
    # column names; no user input enters the SELECT. The f-string is
    # purely a static-string assembly so the column list stays in
    # lock-step with `canonical.AUDIT_ROW_FIELDS`. The `id > ?` clause
    # binds the cursor through a parameter — no SQL string assembly
    # from operator input.
    select_cols = ", ".join(_ROW_COLUMNS) + ", chain_hash"
    if start_after_row_id is None:
        sql = f"SELECT {select_cols} FROM mcp_audit ORDER BY id ASC"  # nosec B608 — column list hardcoded
        cursor = conn.execute(sql)
    else:
        sql = (
            f"SELECT {select_cols} FROM mcp_audit "  # nosec B608 — column list hardcoded
            "WHERE id > ? ORDER BY id ASC"
        )
        cursor = conn.execute(sql, (start_after_row_id,))
    for row in cursor:
        actual_chain = bytes(row["chain_hash"])
        try:
            canonical_dict = _row_to_canonical_dict(row)
            canonical = canonical_audit_row(canonical_dict)
        except ValueError as exc:
            yield ChainMismatch(
                row_id=row["id"],
                expected_hex=f"<canonicalisation-error: {exc}>",
                actual_hex=actual_chain.hex(),
            )
            if not full:
                return
            # Cannot recompute the expected chain for this row, but
            # advance prev_chain with the stored value so subsequent
            # rows can still be validated against the writer's path.
            prev_chain = actual_chain
            continue
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


class SinceCursorError(ValueError):
    """Raised by `resolve_since_cursor` when the spec is well-formed
    but does not resolve to a single trust-anchor row (no match,
    ambiguous match). Distinct from a malformed spec (caller raises
    its own `ValueError` for that, via `parse_since`).
    """


def resolve_since_cursor(
    conn: sqlite3.Connection,
    since: str,
) -> int:
    """Resolve `--since <spec>` to a trust-anchor row id.

    Accepts three forms:
      - Hex prefix of `chain_hash` (≥8 lowercase hex chars, git-like
        convention). Matches the unique audit row whose stored
        `chain_hash` begins with the prefix; ambiguous matches raise
        `SinceCursorError`. The matched row IS the cursor (i.e., the
        walk will verify rows after it, treating its stored hash as
        the trusted baseline).
      - Compact duration (`5m`, `2h`, `7d`) — delegates to
        `parse_since` from `observability.tail`. The cursor is the
        latest audit row strictly before the threshold; the walk
        verifies rows at or after the threshold.
      - ISO 8601 timestamp with timezone — same as duration but
        absolute. Naive timestamps are rejected by `parse_since`.

    Returns the cursor row id. The cursor's own integrity is NOT
    verified by the subsequent walk — operator must have archived a
    trusted copy of `chain_hash[cursor]` separately. Raises
    `SinceCursorError` if the spec cannot resolve to a single row;
    raises `ValueError` (via `parse_since`) for malformed input.
    """
    import re

    from schemabrain.observability import parse_since

    # Hex-prefix detection: 8+ lowercase hex chars, no other chars.
    # 8 chars = 4 bytes of chain_hash (16 bits of disambiguation per
    # char of prefix). Below 8 chars the false-positive rate on a
    # busy audit log climbs fast; refuse to accept shorter prefixes
    # rather than silently picking the lowest-id match.
    is_hex_prefix = re.fullmatch(r"[0-9a-f]{8,}", since) is not None
    if is_hex_prefix:
        # `?||` style GLOB binds the user value through a parameter;
        # no SQL string assembly from operator input. `lower(hex(...))`
        # gives the same hex shape Python's `.hex()` produces, so an
        # operator who copy-pastes a hash hex from CLI output gets
        # the natural match.
        matches = conn.execute(
            "SELECT id FROM mcp_audit WHERE lower(hex(chain_hash)) GLOB ? ORDER BY id ASC LIMIT 2",
            (f"{since}*",),
        ).fetchall()
        if not matches:
            raise SinceCursorError(
                f"--since {since!r}: no audit row has a chain_hash starting with that prefix"
            )
        if len(matches) > 1:
            raise SinceCursorError(
                f"--since {since!r}: prefix matches multiple audit rows "
                f"(ids {matches[0]['id']}, {matches[1]['id']}, ...); "
                f"pass more characters to disambiguate"
            )
        return int(matches[0]["id"])

    # Not a hex prefix → delegate to the duration / ISO parser.
    # `parse_since` raises `ValueError` for malformed input; let it
    # propagate verbatim so the CLI surface error matches `audit list`
    # which already uses the same helper.
    threshold = parse_since(since)
    threshold_iso = threshold.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # Cursor row: the LATEST audit row strictly BEFORE the threshold.
    # The subsequent walk verifies rows at or after the threshold,
    # treating the cursor's stored chain_hash as the trusted baseline.
    # If no row precedes the threshold, raise rather than silently
    # walking the entire chain from genesis — the operator's intent
    # was "verify from this cursor forward", and we cannot honour it
    # without a cursor row.
    cursor_row = conn.execute(
        "SELECT id FROM mcp_audit WHERE occurred_at < ? ORDER BY id DESC LIMIT 1",
        (threshold_iso,),
    ).fetchone()
    if cursor_row is None:
        raise SinceCursorError(
            f"--since {since!r}: no audit row precedes that threshold "
            f"({threshold_iso}); cannot anchor the verification walk"
        )
    return int(cursor_row["id"])
