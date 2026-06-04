"""Generate the cross-language Merkle test vectors.

Writes ``tests/audit/merkle_vectors.json`` — the single source of truth
both ``tests/audit/test_audit_merkle.py`` (Python) and
``web/lib/merkle.test.ts`` (TypeScript) read, so the two implementations
of the RFC-6962 walk cannot silently drift. Any change to
``schemabrain/audit/merkle.py``'s algorithm or prefixes must be followed
by re-running this script and committing the regenerated file; the
Python fixture-reproduction test fails loudly until it is.

Leaves are deterministic synthetic byte strings (``leaf-0``, ``leaf-1``,
…) — the Merkle math is independent of what a leaf preimage is, so this
keeps the vectors decoupled from the audit-row canonical format (which
is pinned separately by the lock-step test).

Usage::

    python scripts/gen_merkle_vectors.py
"""

from __future__ import annotations

import json
from pathlib import Path

from schemabrain.audit.merkle import inclusion_proof, leaf_hash, merkle_root

# Tree sizes covering: empty, single, balanced (2,4,8), and the
# unbalanced border cases (3,5,7) that exercise the fn/sn collapse.
TREE_SIZES = [0, 1, 2, 3, 4, 5, 7, 8]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "tests" / "audit" / "merkle_vectors.json"


def _leaves(n: int) -> list[bytes]:
    return [f"leaf-{i}".encode() for i in range(n)]


def build_vectors() -> dict[str, object]:
    trees: list[dict[str, object]] = []
    for n in TREE_SIZES:
        leaves = _leaves(n)
        proofs = [
            {
                "m": m,
                "leaf_hash_hex": leaf_hash(leaves[m]).hex(),
                "audit_path": [h.hex() for h in inclusion_proof(m, leaves)],
            }
            for m in range(n)
        ]
        trees.append(
            {
                "n": n,
                "leaves_hex": [d.hex() for d in leaves],
                "root_hex": merkle_root(leaves).hex(),
                "proofs": proofs,
            }
        )
    return {
        "description": (
            "RFC-6962 Merkle vectors for schemabrain audit. Synthetic "
            "leaves (leaf-N). Regenerate with scripts/gen_merkle_vectors.py."
        ),
        "trees": trees,
    }


def main() -> None:
    vectors = build_vectors()
    OUTPUT_PATH.write_text(json.dumps(vectors, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_PATH} ({len(vectors['trees'])} trees)")


if __name__ == "__main__":
    main()
