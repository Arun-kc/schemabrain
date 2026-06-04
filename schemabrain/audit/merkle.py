"""Derived RFC-6962 Merkle tree over the `mcp_audit` hash chain.

The append-only chain (`chain.py`) answers "has any row been rewritten
since it was written?" by walking stored-vs-recomputed `chain_hash`
values. This module adds a complementary, *derived* answer: a single
Merkle Tree Hash (the root) committing to every row, plus O(log n)
inclusion proofs so a client can independently confirm that one
specific row reconciles to that one root — without re-reading the whole
log.

ADR 0001 / RFC 6962 §2.1. The root is **detection, not prevention**,
exactly like the chain (`chain.py:6-13`): an attacker with write access
to the SQLite file can rewrite the rows and the recomputed root
coherently. The defence is an externally archived prior root, which
SchemaBrain does not (yet) persist — so nothing here is "tamper-proof".
The honest claim the root supports is narrow and true: *every row
reconciles to one self-consistent root, computed now.*

Two load-bearing invariants:

1. **Leaf preimage == chain preimage.** A leaf's bytes are exactly
   `canonical_audit_row(_row_to_canonical_dict(row))` — the same
   serialisation the chain consumes (`verify.py:126-127`). The tree
   reuses that path rather than re-implementing it, so the chain and the
   tree can never silently disagree on what a row's bytes are. The only
   difference is domain separation: a leaf hash prepends `0x00`, an
   internal node prepends `0x01` (RFC 6962 §2.1), whereas the chain
   prepends nothing — so `leaf_hash(bytes) != chain_hash` even for the
   same bytes, which is intentional (different trees of trust).

2. **id-ASC order.** Leaves are ordered by `id` ASC, matching the
   chain walk (`verify.py:115`). `leaf_index` is the 0-based position in
   id-ASC order. The dashboard rows route renders id DESC — callers must
   never derive a leaf index from render position.

Pure: stdlib `hashlib` only, no third-party dependency, no sqlite. The
DB adapter that reads canonical leaves in id-ASC order lives next to the
sidecar route; this module operates only over `list[bytes]`.
"""

from __future__ import annotations

import hashlib
from typing import Final

# RFC 6962 §2.1 domain-separation prefixes. Raw single bytes, NOT ASCII
# digits — `b"\x00"` not `b"0"`. The prefix is what keeps a leaf hash
# distinct from an internal-node hash (and from the chain hash, which
# uses no prefix).
LEAF_PREFIX: Final[bytes] = b"\x00"
NODE_PREFIX: Final[bytes] = b"\x01"

# MTH({}) — the Merkle Tree Hash of the empty tree is the SHA-256 of the
# empty string (RFC 6962 §2.1). Surfaced as the root of an empty audit
# log; never rendered to an operator as a "verified" claim.
EMPTY_TREE_HASH: Final[bytes] = hashlib.sha256(b"").digest()

_DIGEST_SIZE: Final[int] = 32


def leaf_hash(leaf_bytes: bytes) -> bytes:
    """Return the RFC-6962 leaf hash `SHA-256(0x00 || leaf_bytes)`.

    `leaf_bytes` MUST be the output of
    `canonical_audit_row(_row_to_canonical_dict(row))` so the tree and
    the chain consume byte-identical preimages. This function does not
    re-serialise — it only prepends the leaf domain prefix and hashes.
    """
    return hashlib.sha256(LEAF_PREFIX + leaf_bytes).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    """Return the RFC-6962 internal-node hash `SHA-256(0x01 || L || R)`.

    Both children are 32-byte digests (leaf hashes or other node hashes).
    """
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def _largest_power_of_two_below(n: int) -> int:
    """Largest power of two STRICTLY LESS THAN `n`, for `n >= 2`.

    RFC 6962's split point `k`: `k < n <= 2k`. e.g. n=2->1, n=3->2,
    n=4->2, n=5->4, n=8->4, n=9->8. Computed from the highest set bit of
    `n - 1`.
    """
    return 1 << ((n - 1).bit_length() - 1)


def _root_from_leaf_hashes(hashes: list[bytes]) -> bytes:
    """MTH over a list of already-hashed leaves (RFC 6962 §2.1)."""
    n = len(hashes)
    if n == 0:
        return EMPTY_TREE_HASH
    if n == 1:
        return hashes[0]
    k = _largest_power_of_two_below(n)
    return _node_hash(_root_from_leaf_hashes(hashes[:k]), _root_from_leaf_hashes(hashes[k:]))


def _path_from_leaf_hashes(m: int, hashes: list[bytes]) -> list[bytes]:
    """PATH(m, D[n]) over already-hashed leaves (RFC 6962 §2.1.1).

    Returns the audit path — sibling hashes in leaf->root order, no
    direction flags. `m` is the 0-based leaf index.
    """
    n = len(hashes)
    if not 0 <= m < n:
        raise ValueError(f"leaf index {m} out of range for tree size {n}")
    if n == 1:
        # The leaf is the whole tree — empty path.
        return []
    k = _largest_power_of_two_below(n)
    if m < k:
        # Target is in the left subtree; sibling is the right subtree root.
        return [*_path_from_leaf_hashes(m, hashes[:k]), _root_from_leaf_hashes(hashes[k:])]
    # Target is in the right subtree; sibling is the left subtree root.
    return [*_path_from_leaf_hashes(m - k, hashes[k:]), _root_from_leaf_hashes(hashes[:k])]


def merkle_root(leaves: list[bytes]) -> bytes:
    """Return the 32-byte Merkle root over leaf PREIMAGES in id-ASC order.

    `leaves` is the list of `canonical_audit_row(...)` byte strings.
    Empty list -> `EMPTY_TREE_HASH`. A single leaf -> its leaf hash.
    """
    return _root_from_leaf_hashes([leaf_hash(d) for d in leaves])


def inclusion_proof(leaf_index: int, leaves: list[bytes]) -> list[bytes]:
    """Return the audit path proving `leaves[leaf_index]` is in the tree.

    Sibling hashes in leaf->root order (RFC 6962 §2.1.1 PATH). Raises
    `ValueError` if `leaf_index` is out of range. The path is empty for a
    single-leaf tree (the leaf is the root).
    """
    return _path_from_leaf_hashes(leaf_index, [leaf_hash(d) for d in leaves])


def verify_inclusion(
    leaf_index: int,
    tree_size: int,
    leaf_hash_value: bytes,
    audit_path: list[bytes],
    root: bytes,
) -> bool:
    """Verify an inclusion proof (RFC 6962 §2.1.1 verification walk).

    Returns True iff folding `leaf_hash_value` up through `audit_path`
    reconstructs `root` for a leaf at `leaf_index` in a tree of
    `tree_size` leaves. Direction (left/right child) is recovered from
    the index via the `fn`/`sn` walk, so `audit_path` carries no
    direction flags. Never raises on malformed input — a bad index,
    wrong-length path, or corrupted hash returns False — so a tampered
    server response can never pass verification.

    Mirrored byte-for-byte by `web/lib/merkle.ts::verifyInclusion`; the
    cross-language parity fixture pins that they agree.
    """
    if leaf_index < 0 or tree_size <= 0 or leaf_index >= tree_size:
        return False
    fn = leaf_index
    sn = tree_size - 1
    r = leaf_hash_value
    for sibling in audit_path:
        if sn == 0:
            # More path entries than the tree depth allows for this index.
            return False
        if (fn & 1) or fn == sn:
            # Current node is a right child or the rightmost (border)
            # node at its level — the incoming sibling sits on the LEFT.
            r = _node_hash(sibling, r)
            if not (fn & 1):
                # Border node: collapse the run of trailing zero bits so
                # levels skipped for want of a sibling advance correctly.
                # This is what makes unbalanced trees (n=3,5,6,7,...) verify.
                while (fn & 1) == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            # Current node is a left child — sibling sits on the RIGHT.
            r = _node_hash(r, sibling)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root
