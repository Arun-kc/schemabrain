"""Fingerprint primitive for `mcp_audit` rows.

The fingerprint is a 32-byte sha256 digest over a small, frozen set of
structural fields. Per ADR 0001 ("Privacy-by-construction"), the
fingerprint contains no row content, no column values, and no
identifying schema info; the dataclass below has no field that could
carry such content.

The dataclass field count is pinned by a CI test
(`tests/audit/test_audit_fingerprint.py::TestFieldCount`). Adding a
field is therefore a deliberate, reviewed action — the failing test
forces the addition through review rather than landing as a silent
schema drift.

`FINGERPRINT_VERSION` is stamped on every audit row at write time so
the audit table can carry rows produced under multiple formula
versions simultaneously. Verification + downstream aggregation read
both the fingerprint AND its version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final, Literal

from schemabrain.pii.categories import PIICategory

# Version string stamped on every audit row's `fingerprint_version`
# column. Bumped only when the formula or input set changes.
FINGERPRINT_VERSION: Final[str] = "fp-v1"

CostClass = Literal["small", "medium", "large", "refused"]

RefusalReason = Literal[
    "pii_blocked",
    "allowlist_violation",
    "fragment_unsafe",
    "cost_cap_exceeded",
    "ambiguous_resolution",
    "schema_drift",
]


@dataclass(frozen=True, slots=True)
class FingerprintInput:
    """The complete, frozen input to `compute_fingerprint`.

    Field count is invariant — see the module docstring for the
    privacy rationale. Construction is the only way to assemble an
    input; immutability prevents post-hoc mutation.
    """

    ast_shape_hash: bytes | None
    pii_tags_touched: frozenset[PIICategory]
    refusal_reason: RefusalReason | None
    cost_class: CostClass
    rule_id: str | None


def compute_fingerprint(fp_input: FingerprintInput) -> bytes:
    """Return the 32-byte sha256 digest of a canonical serialisation
    of `fp_input`.

    Stable across deployments: two deployments that build a
    semantically-identical `FingerprintInput` produce the IDENTICAL
    digest, regardless of database, schema, or workload.
    """
    canonical = json.dumps(
        {
            "v": FINGERPRINT_VERSION,
            "ast": fp_input.ast_shape_hash.hex() if fp_input.ast_shape_hash is not None else None,
            "pii": sorted(fp_input.pii_tags_touched),
            "reason": fp_input.refusal_reason,
            "cost": fp_input.cost_class,
            "rule": fp_input.rule_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest()
