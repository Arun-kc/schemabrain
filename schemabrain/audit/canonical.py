"""Canonical serialisation of `mcp_audit` row content fields.

The 13 content fields (everything except `chain_hash`) are serialised
to deterministic UTF-8 JSON bytes. The output is the input to both
`compute_chain_hash` and any future tamper-evidence check, so any drift
in this function silently invalidates every previously chained row.
Tests pin determinism, key ordering, binary-field handling, and the
strict 13-field shape so a regression fails loudly.

Binary fields (`ast_shape_hash`, `fingerprint`) round-trip through
lowercase hex so the JSON payload stays text-only. None values pass
through as JSON `null`. Non-ASCII strings encode as their UTF-8
codepoints (`ensure_ascii=False`) so a column name containing `ä`
hashes as `0xc3 0xa4`, not as `\\u00e4`.

The chain itself lives in `chain.py`; the schema lives in `ddl.py`.
"""

from __future__ import annotations

import json
from typing import Any, Final

# Pinned 13-field set. Mirrors ADR 0001's DDL columns 1..13 (everything
# except `chain_hash`, which is the output of the chain computation and
# therefore not part of its input). Order does not matter for the
# canonical form — `json.dumps(..., sort_keys=True)` re-sorts — but the
# set membership is load-bearing. Adding a field requires a coordinated
# schema bump and chain re-verification.
AUDIT_ROW_FIELDS: Final[frozenset[str]] = frozenset(
    {
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
    }
)

# Fields that may carry None per ADR 0001. Everything else must be
# populated when canonicalising — the SQL CHECK and NOT NULL
# constraints would reject the row at write time anyway; failing at
# canonicalisation surfaces the bug earlier.
_NULLABLE: Final[frozenset[str]] = frozenset(
    {"caller_id", "refusal_reason", "ast_shape_hash", "rule_id"}
)

# Binary fields rendered as lowercase hex strings in the JSON payload.
_BINARY: Final[frozenset[str]] = frozenset({"ast_shape_hash", "fingerprint"})

_FINGERPRINT_BYTES: Final[int] = 32
_AST_SHAPE_HASH_BYTES: Final[int] = 32


def canonical_audit_row(row: dict[str, Any]) -> bytes:
    """Return the deterministic UTF-8 JSON bytes for one audit row.

    Strict-shape: `row` MUST contain every key in `AUDIT_ROW_FIELDS`
    and NO other keys. Caller bugs (extra column, missing column,
    wrongly-typed bytes) raise `ValueError` rather than silently
    producing a malformed canonical form that would invalidate the
    chain on the next verify.
    """
    missing = AUDIT_ROW_FIELDS - row.keys()
    if missing:
        raise ValueError(f"canonical_audit_row missing required fields: {sorted(missing)}")
    extra = row.keys() - AUDIT_ROW_FIELDS
    if extra:
        raise ValueError(f"canonical_audit_row got unexpected fields: {sorted(extra)}")

    payload: dict[str, Any] = {}
    for key in AUDIT_ROW_FIELDS:
        value = row[key]
        if value is None:
            if key not in _NULLABLE:
                raise ValueError(
                    f"canonical_audit_row field {key!r} cannot be None "
                    f"(only {sorted(_NULLABLE)} are nullable)"
                )
            payload[key] = None
            continue
        if key in _BINARY:
            payload[key] = _hex_bytes(key, value)
        else:
            payload[key] = value

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hex_bytes(field: str, value: Any) -> str:
    if not isinstance(value, bytes):
        raise ValueError(
            f"canonical_audit_row field {field!r} must be bytes or None, got {type(value).__name__}"
        )
    expected = _AST_SHAPE_HASH_BYTES if field == "ast_shape_hash" else _FINGERPRINT_BYTES
    if len(value) != expected:
        raise ValueError(
            f"canonical_audit_row field {field!r} must be exactly "
            f"{expected} bytes, got {len(value)}"
        )
    return value.hex()
