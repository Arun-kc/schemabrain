"""Tests for the per-row chain hash primitive.

`chain_hash[N] = sha256(chain_hash[N-1] || canonical(row[N]))`. The
genesis row uses 32 zero bytes for the previous hash. Tests pin the
genesis constant, the formula, the determinism, and the
collision-distinguishability of the inputs.
"""

from __future__ import annotations

import hashlib

import pytest

from schemabrain.audit.canonical import canonical_audit_row
from schemabrain.audit.chain import (
    CHAIN_HASH_SIZE,
    GENESIS_CHAIN_HASH,
    compute_chain_hash,
)


def _row(**overrides: object) -> dict[str, object]:
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


class TestGenesis:
    def test_genesis_is_thirty_two_zero_bytes(self) -> None:
        assert GENESIS_CHAIN_HASH == b"\x00" * 32

    def test_chain_hash_size_constant(self) -> None:
        assert CHAIN_HASH_SIZE == 32


class TestComputeShape:
    def test_returns_thirty_two_bytes(self) -> None:
        out = compute_chain_hash(GENESIS_CHAIN_HASH, canonical_audit_row(_row()))
        assert isinstance(out, bytes)
        assert len(out) == CHAIN_HASH_SIZE

    def test_formula_matches_explicit_sha256(self) -> None:
        """Pin the EXACT formula: sha256(prev || canonical). If a future
        refactor switches to BLAKE2 or HMAC, this test fails loudly
        rather than producing a silent chain-shape break."""
        prev = GENESIS_CHAIN_HASH
        canonical = canonical_audit_row(_row())
        expected = hashlib.sha256(prev + canonical).digest()
        assert compute_chain_hash(prev, canonical) == expected


class TestDeterminism:
    def test_same_inputs_same_output(self) -> None:
        canonical = canonical_audit_row(_row())
        out1 = compute_chain_hash(GENESIS_CHAIN_HASH, canonical)
        out2 = compute_chain_hash(GENESIS_CHAIN_HASH, canonical)
        assert out1 == out2

    def test_prev_hash_change_propagates(self) -> None:
        canonical = canonical_audit_row(_row())
        out_genesis = compute_chain_hash(GENESIS_CHAIN_HASH, canonical)
        out_other = compute_chain_hash(b"\x01" * 32, canonical)
        assert out_genesis != out_other

    def test_row_change_propagates(self) -> None:
        row_a = canonical_audit_row(_row(tool_name="describe_table"))
        row_b = canonical_audit_row(_row(tool_name="describe_column"))
        out_a = compute_chain_hash(GENESIS_CHAIN_HASH, row_a)
        out_b = compute_chain_hash(GENESIS_CHAIN_HASH, row_b)
        assert out_a != out_b


class TestInputValidation:
    def test_wrong_size_prev_chain_hash_rejected(self) -> None:
        """The chain depends on a fixed-width previous hash. A
        misshapen prev (truncation, accidentally passing a hex string
        decoded wrong) silently changes the hash space — refuse so the
        bug is visible at the call site."""
        with pytest.raises(ValueError, match="32 bytes"):
            compute_chain_hash(b"\x00" * 16, b"x")


class TestChainAdvances:
    def test_two_row_sequence(self) -> None:
        """A two-row sequence: chain[2] depends on chain[1] which depends
        on genesis. Walk the chain explicitly so the verification path
        can match this structure 1:1."""
        row1 = _row(id=1, tool_name="describe_table")
        row2 = _row(id=2, tool_name="describe_column")
        c1 = canonical_audit_row(row1)
        c2 = canonical_audit_row(row2)
        chain1 = compute_chain_hash(GENESIS_CHAIN_HASH, c1)
        chain2 = compute_chain_hash(chain1, c2)
        # Manual recomputation should match.
        expected_chain1 = hashlib.sha256(GENESIS_CHAIN_HASH + c1).digest()
        expected_chain2 = hashlib.sha256(expected_chain1 + c2).digest()
        assert chain1 == expected_chain1
        assert chain2 == expected_chain2

    def test_identical_back_to_back_calls_produce_distinct_chains(self) -> None:
        """Even when two adjacent rows have the same fingerprint (the v1
        default — all rows fingerprint identically until v2 brings
        differentiation), the chain still advances because canonical
        bytes include `id` and `occurred_at`."""
        row1 = _row(id=1, occurred_at="2026-05-17T18:00:00.000000Z")
        row2 = _row(id=2, occurred_at="2026-05-17T18:00:00.000001Z")
        c1 = canonical_audit_row(row1)
        c2 = canonical_audit_row(row2)
        chain1 = compute_chain_hash(GENESIS_CHAIN_HASH, c1)
        chain2 = compute_chain_hash(chain1, c2)
        assert chain1 != chain2
