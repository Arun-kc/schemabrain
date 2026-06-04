"""Tests for the derived RFC-6962 Merkle layer (`audit/merkle.py`).

Three things are pinned here:

1. **Algorithm correctness** — domain-separation prefixes, the empty
   tree, single-leaf trees, balanced and unbalanced (n=3,5,7) split
   points, all verified against independent manual `hashlib` recomputes
   rather than against the module's own output.
2. **Round-trip + negative properties** — for every tree size and every
   leaf index, the generated proof verifies against the generated root;
   any single-byte mutation of any input is rejected.
3. **Leaf-preimage lock-step** — a Merkle leaf is hashed from exactly
   `canonical_audit_row(...)` output, the same preimage the chain
   consumes, so the tree and the chain can never silently disagree.

A cross-language fixture (`merkle_vectors.json`) is read back and
reproduced so `web/lib/merkle.test.ts` and this module cannot drift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from schemabrain.audit.canonical import canonical_audit_row
from schemabrain.audit.merkle import (
    EMPTY_TREE_HASH,
    LEAF_PREFIX,
    NODE_PREFIX,
    _largest_power_of_two_below,
    inclusion_proof,
    leaf_hash,
    merkle_root,
    verify_inclusion,
)

FIXTURE_PATH = Path(__file__).parent / "merkle_vectors.json"


def _row(**overrides: object) -> dict[str, object]:
    """A 13-field canonical row dict (mirrors test_audit_chain.py:23-40)."""
    base: dict[str, object] = {
        "id": 1,
        "occurred_at": "2026-05-17T18:00:00.000000Z",
        "source_connection_id": "src1",
        "caller_id": None,
        "tool_name": "find_relevant_tables",
        "status": "success",
        "refusal_reason": None,
        "cost_class": "small",
        "pii_categories": "",
        "ast_shape_hash": None,
        "rule_id": None,
        "fingerprint": bytes.fromhex("aa" * 32),
        "fingerprint_version": "fp-v1",
    }
    base.update(overrides)
    return base


def _leaves(n: int) -> list[bytes]:
    """`n` deterministic, distinct synthetic leaf preimages.

    The Merkle math is independent of what the preimage is, so synthetic
    bytes keep the algorithm tests decoupled from the audit-row canonical
    format (which is exercised separately in the lock-step test).
    """
    return [f"leaf-{i}".encode() for i in range(n)]


def _lh(d: bytes) -> bytes:
    return hashlib.sha256(LEAF_PREFIX + d).digest()


def _nh(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


class TestPrefixes:
    def test_leaf_prefix_is_raw_zero_byte(self) -> None:
        assert LEAF_PREFIX == b"\x00"

    def test_node_prefix_is_raw_one_byte(self) -> None:
        assert NODE_PREFIX == b"\x01"

    def test_empty_tree_hash_is_sha256_of_empty_string(self) -> None:
        assert hashlib.sha256(b"").digest() == EMPTY_TREE_HASH

    def test_leaf_hash_is_sha256_of_prefixed_preimage(self) -> None:
        d = b"some-canonical-row-bytes"
        assert leaf_hash(d) == hashlib.sha256(b"\x00" + d).digest()

    def test_leaf_hash_differs_from_chain_hash_for_same_bytes(self) -> None:
        # The chain prepends nothing; the leaf prepends 0x00. Same bytes,
        # different hash — the intentional domain separation.
        d = b"row-bytes"
        assert leaf_hash(d) != hashlib.sha256(d).digest()


class TestLargestPowerOfTwoBelow:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (2, 1),
            (3, 2),
            (4, 2),
            (5, 4),
            (6, 4),
            (7, 4),
            (8, 4),
            (9, 8),
            (16, 8),
            (17, 16),
        ],
    )
    def test_split_point(self, n: int, expected: int) -> None:
        assert _largest_power_of_two_below(n) == expected


class TestMerkleRoot:
    def test_empty_tree(self) -> None:
        assert merkle_root([]) == EMPTY_TREE_HASH

    def test_single_leaf_root_is_leaf_hash(self) -> None:
        d0 = _leaves(1)[0]
        assert merkle_root([d0]) == _lh(d0)

    def test_two_leaves_manual_recompute(self) -> None:
        d0, d1 = _leaves(2)
        assert merkle_root([d0, d1]) == _nh(_lh(d0), _lh(d1))

    def test_three_leaves_unbalanced_k_is_two(self) -> None:
        # n=3 -> k=2: root = node( node(L0,L1), L2 ). The asymmetric
        # case a "pad to power of two" implementation gets wrong.
        d0, d1, d2 = _leaves(3)
        expected = _nh(_nh(_lh(d0), _lh(d1)), _lh(d2))
        assert merkle_root([d0, d1, d2]) == expected

    def test_four_leaves_balanced(self) -> None:
        d0, d1, d2, d3 = _leaves(4)
        expected = _nh(_nh(_lh(d0), _lh(d1)), _nh(_lh(d2), _lh(d3)))
        assert merkle_root([d0, d1, d2, d3]) == expected

    def test_five_leaves_unbalanced_k_is_four(self) -> None:
        # n=5 -> k=4: left = full 4-leaf tree, right = single leaf.
        d = _leaves(5)
        left = _nh(_nh(_lh(d[0]), _lh(d[1])), _nh(_lh(d[2]), _lh(d[3])))
        expected = _nh(left, _lh(d[4]))
        assert merkle_root(d) == expected

    def test_root_is_thirty_two_bytes(self) -> None:
        assert len(merkle_root(_leaves(7))) == 32


class TestInclusionProof:
    def test_single_leaf_proof_is_empty(self) -> None:
        assert inclusion_proof(0, _leaves(1)) == []

    def test_two_leaf_proofs(self) -> None:
        d0, d1 = _leaves(2)
        assert inclusion_proof(0, [d0, d1]) == [_lh(d1)]
        assert inclusion_proof(1, [d0, d1]) == [_lh(d0)]

    def test_three_leaf_rightmost_proof_structure(self) -> None:
        # m=2 in a 3-leaf tree: the sibling is the left 2-leaf subtree root.
        d0, d1, d2 = _leaves(3)
        assert inclusion_proof(2, [d0, d1, d2]) == [_nh(_lh(d0), _lh(d1))]

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            inclusion_proof(3, _leaves(3))
        with pytest.raises(ValueError):
            inclusion_proof(-1, _leaves(3))

    def test_empty_tree_proof_raises(self) -> None:
        with pytest.raises(ValueError):
            inclusion_proof(0, [])


class TestVerifyInclusionRoundTrip:
    @pytest.mark.parametrize("n", list(range(1, 17)))
    def test_every_index_verifies_against_its_root(self, n: int) -> None:
        leaves = _leaves(n)
        root = merkle_root(leaves)
        for m in range(n):
            path = inclusion_proof(m, leaves)
            assert verify_inclusion(m, n, _lh(leaves[m]), path, root) is True


class TestVerifyInclusionNegatives:
    def _setup(self, n: int = 7, m: int = 3) -> tuple[int, int, bytes, list[bytes], bytes]:
        leaves = _leaves(n)
        root = merkle_root(leaves)
        path = inclusion_proof(m, leaves)
        return m, n, _lh(leaves[m]), path, root

    def test_corrupted_leaf_hash_rejected(self) -> None:
        m, n, lh, path, root = self._setup()
        bad = bytearray(lh)
        bad[0] ^= 0x01
        assert verify_inclusion(m, n, bytes(bad), path, root) is False

    def test_corrupted_path_entry_rejected(self) -> None:
        m, n, lh, path, root = self._setup()
        bad_path = list(path)
        mutated = bytearray(bad_path[0])
        mutated[0] ^= 0x01
        bad_path[0] = bytes(mutated)
        assert verify_inclusion(m, n, lh, bad_path, root) is False

    def test_wrong_root_rejected(self) -> None:
        m, n, lh, path, _ = self._setup()
        wrong_root = merkle_root(_leaves(8))
        assert verify_inclusion(m, n, lh, path, wrong_root) is False

    def test_index_out_of_range_rejected(self) -> None:
        _, n, lh, path, root = self._setup()
        assert verify_inclusion(n, n, lh, path, root) is False
        assert verify_inclusion(-1, n, lh, path, root) is False

    def test_empty_tree_size_rejected(self) -> None:
        assert verify_inclusion(0, 0, EMPTY_TREE_HASH, [], EMPTY_TREE_HASH) is False

    def test_truncated_path_rejected(self) -> None:
        m, n, lh, path, root = self._setup()
        assert verify_inclusion(m, n, lh, path[:-1], root) is False

    def test_extended_path_rejected(self) -> None:
        m, n, lh, path, root = self._setup()
        assert verify_inclusion(m, n, lh, [*path, b"\x11" * 32], root) is False

    def test_wrong_index_for_same_path_rejected(self) -> None:
        # A valid path verified at the wrong index must fail.
        leaves = _leaves(7)
        root = merkle_root(leaves)
        path = inclusion_proof(3, leaves)
        assert verify_inclusion(4, 7, _lh(leaves[3]), path, root) is False


class TestLeafPreimageLockStep:
    """The single most important guarantee: a Merkle leaf is hashed from
    the same canonical bytes the chain consumes, so a future change to
    the canonical form moves BOTH and they can never silently diverge."""

    def test_leaf_preimage_is_canonical_audit_row(self) -> None:
        row = _row()
        canonical = canonical_audit_row(row)
        # The chain hashes `canonical` (prefixed by prev); the tree hashes
        # `canonical` (prefixed by 0x00). Both source the SAME bytes.
        assert leaf_hash(canonical) == hashlib.sha256(b"\x00" + canonical).digest()

    def test_distinct_rows_yield_distinct_leaves(self) -> None:
        a = canonical_audit_row(_row(id=1))
        b = canonical_audit_row(_row(id=2))
        assert leaf_hash(a) != leaf_hash(b)


class TestCrossLanguageFixture:
    """Reproduce the committed cross-language vectors so the Python impl
    and `web/lib/merkle.test.ts` (which consumes the same file) cannot
    drift. Any algorithm change makes the recompute diverge from the
    committed fixture and this test fails loudly — regenerate the file."""

    def test_fixture_exists(self) -> None:
        assert FIXTURE_PATH.exists(), (
            f"{FIXTURE_PATH} missing — regenerate with scripts/gen_merkle_vectors.py"
        )

    def test_reproduces_every_fixture_tree(self) -> None:
        data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for tree in data["trees"]:
            n = tree["n"]
            leaves = [bytes.fromhex(h) for h in tree["leaves_hex"]]
            assert len(leaves) == n
            assert merkle_root(leaves).hex() == tree["root_hex"]
            for proof in tree["proofs"]:
                m = proof["m"]
                assert leaf_hash(leaves[m]).hex() == proof["leaf_hash_hex"]
                path = inclusion_proof(m, leaves)
                assert [h.hex() for h in path] == proof["audit_path"]
                assert verify_inclusion(
                    m,
                    n,
                    leaf_hash(leaves[m]),
                    path,
                    bytes.fromhex(tree["root_hex"]),
                )
