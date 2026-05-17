"""Audit substrate for the `mcp_audit` append-only table.

Per ADR 0001 (`docs/adr/0001-audit-row-and-pii-taxonomy.md`), every
MCP tool invocation that completes a charter-defined status writes
exactly one row to `mcp_audit`. This package owns the DDL, the
canonical row serialisation, and the per-row sha256 chain hash that
makes tampering detectable against any external archive that captured
a prior `chain_hash`.

The fingerprint primitive lands alongside this module and feeds the
`fingerprint` column of every row. Its privacy guarantee is documented
in ADR 0001's "Privacy-by-construction" section; the field count is
pinned in the dataclass definition itself.
"""

from __future__ import annotations

from schemabrain.audit.canonical import AUDIT_ROW_FIELDS, canonical_audit_row
from schemabrain.audit.chain import (
    CHAIN_HASH_SIZE,
    GENESIS_CHAIN_HASH,
    compute_chain_hash,
)
from schemabrain.audit.ddl import ensure_audit_schema

__all__ = [
    "AUDIT_ROW_FIELDS",
    "CHAIN_HASH_SIZE",
    "GENESIS_CHAIN_HASH",
    "canonical_audit_row",
    "compute_chain_hash",
    "ensure_audit_schema",
]
