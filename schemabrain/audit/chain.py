"""Per-row sha256 chain hash for the `mcp_audit` table.

For row N: `chain_hash[N] = sha256(chain_hash[N-1] || canonical(row[N]))`.
The genesis row uses 32 zero bytes for the previous hash.

ADR 0001 Mechanism C: the chain is detection, not prevention. An
attacker with write access to the SQLite file can rewrite the chain
coherently — the defence is that any external archive (file-system
snapshot, S3 backup, off-host copy) that captured a prior `chain_hash`
can be compared against the current row to detect tampering between the
snapshot and now. `schemabrain audit verify` walks the chain locally to
detect in-file mismatches (a tampered row whose author forgot to
rewrite subsequent chain hashes).

This module owns ONLY the hash primitive. Building the row dict lives
in `writer.py`; the canonical serialisation lives in `canonical.py`.
"""

from __future__ import annotations

import hashlib
from typing import Final

CHAIN_HASH_SIZE: Final[int] = 32

# 32 zero bytes — the sentinel used as `chain_hash[0]`'s "previous"
# value. Matches the ADR-documented genesis convention so a verifier
# starting from row 1 walks the same hash structure as the writer.
GENESIS_CHAIN_HASH: Final[bytes] = b"\x00" * CHAIN_HASH_SIZE


def compute_chain_hash(prev_chain_hash: bytes, canonical_row: bytes) -> bytes:
    """Return the 32-byte chain hash for one audit row.

    Both inputs are bytes; the output is `sha256(prev || canonical)`'s
    digest. The caller is responsible for passing the previous row's
    `chain_hash` (or `GENESIS_CHAIN_HASH` for the genesis row) and the
    output of `canonical_audit_row(...)`.
    """
    if len(prev_chain_hash) != CHAIN_HASH_SIZE:
        raise ValueError(
            f"prev_chain_hash must be exactly {CHAIN_HASH_SIZE} bytes, got {len(prev_chain_hash)}"
        )
    return hashlib.sha256(prev_chain_hash + canonical_row).digest()
