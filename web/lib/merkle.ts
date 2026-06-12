// Client-side RFC-6962 inclusion-proof verifier — the real cryptography
// behind the /audit "Verify against root" action. Mirrors
// `schemabrain/audit/merkle.py` byte-for-byte (same 0x00 leaf / 0x01 node
// prefixes, same fn/sn walk); the committed cross-language fixture
// (tests/audit/merkle_vectors.json) pins that the two cannot drift.
//
// Framework-free (sibling of lib/refusals.ts / lib/piiMatrix.ts), async
// because crypto.subtle.digest is async.
//
// TRUST BOUNDARY (read this before extending): the browser is GIVEN
// `leaf_hash_hex` by the same server that computed the root. It does NOT
// re-derive the leaf hash from the row's canonical bytes — that would
// require a second, byte-exact reimplementation of `canonical_audit_row`
// in TS that could itself drift and raise false alarms, and it has no
// external anchor anyway (the row content also comes from the server). So
// the honest, real check this module performs is narrow and true:
// recompute the root from (leaf_hash, audit_path) and require it to equal
// the root fetched from the SEPARATE /api/audit/merkle/root call. That
// proves every row reconciles to one self-consistent root the server
// computed — NOT that the underlying data is authentic. The chain is
// detection, not prevention; the real external defence is an archived
// prior root, which this PR does not persist.

import type { MerkleProofResponse } from "@/lib/types";

const LEAF_PREFIX = 0x00;
const NODE_PREFIX = 0x01;
const DIGEST_BYTES = 32;

/** Lowercase-hex string -> bytes. Rejects odd length / non-hex (untrusted
 * input is validated even though the server always sends clean lowercase). */
export function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) {
    throw new Error(`hex string has odd length: ${hex.length}`);
  }
  if (!/^[0-9a-f]*$/.test(hex)) {
    throw new Error("hex string contains non-lowercase-hex characters");
  }
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

/** Bytes -> lowercase hex, byte-for-byte equal to Python `bytes.hex()`. */
export function bytesToHex(bytes: Uint8Array): string {
  let hex = "";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return hex;
}

async function sha256(data: Uint8Array): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest("SHA-256", data as BufferSource);
  return new Uint8Array(digest);
}

/** H(0x01 || left || right) — both children are 32-byte digests. */
async function hashNode(left: Uint8Array, right: Uint8Array): Promise<Uint8Array> {
  const buf = new Uint8Array(1 + left.length + right.length);
  buf[0] = NODE_PREFIX;
  buf.set(left, 1);
  buf.set(right, 1 + left.length);
  return sha256(buf);
}

function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a[i] ^ b[i];
  return diff === 0;
}

/**
 * RFC 6962 §2.1.1 inclusion-proof verification. Folds `leafHash` up
 * through `auditPath` and returns true iff it reconstructs `root` for a
 * leaf at `leafIndex` in a tree of `treeSize`. Direction (left/right
 * child) is recovered from the index via the fn/sn walk, so `auditPath`
 * carries no direction flags. Never throws — a bad index, wrong-length
 * path, or corrupted hash returns false — so a tampered response can
 * never pass verification. Byte-identical to the Python `verify_inclusion`.
 */
export async function verifyInclusion(
  leafIndex: number,
  treeSize: number,
  leafHash: Uint8Array,
  auditPath: readonly Uint8Array[],
  root: Uint8Array,
): Promise<boolean> {
  if (leafIndex < 0 || treeSize <= 0 || leafIndex >= treeSize) return false;
  if (leafHash.length !== DIGEST_BYTES || root.length !== DIGEST_BYTES) return false;
  let fn = leafIndex;
  let sn = treeSize - 1;
  let r = leafHash;
  for (const sibling of auditPath) {
    if (sn === 0) return false; // path longer than the tree depth allows
    if (sibling.length !== DIGEST_BYTES) return false;
    if ((fn & 1) === 1 || fn === sn) {
      // Right child or rightmost border node — sibling on the LEFT.
      r = await hashNode(sibling, r);
      if ((fn & 1) === 0) {
        // Border node: collapse the run of trailing zero bits so skipped
        // levels advance correctly (makes unbalanced trees verify).
        while ((fn & 1) === 0 && fn !== 0) {
          fn >>= 1;
          sn >>= 1;
        }
      }
    } else {
      // Left child — sibling on the RIGHT.
      r = await hashNode(r, sibling);
    }
    fn >>= 1;
    sn >>= 1;
  }
  return sn === 0 && bytesEqual(r, root);
}

/**
 * Verify a proof-route response against a given root hex. Returns true
 * iff the proof's leaf reconciles to `rootHex`. The caller (the Ledger
 * component) passes the SEPARATELY-fetched global root here so a "proven"
 * verdict means "this row reconciles to the one global root computed now"
 * — see the trust-boundary note at the top of this file. A proof rooted
 * at a different tree (e.g. the log grew between fetches) fails the walk
 * and the caller surfaces that as a benign "log advanced" state, not a
 * break.
 */
export async function verifyProofAgainstRoot(
  proof: Pick<MerkleProofResponse, "leaf_index" | "tree_size" | "leaf_hash_hex" | "audit_path">,
  rootHex: string,
): Promise<boolean> {
  return verifyInclusion(
    proof.leaf_index,
    proof.tree_size,
    hexToBytes(proof.leaf_hash_hex),
    proof.audit_path.map(hexToBytes),
    hexToBytes(rootHex),
  );
}
