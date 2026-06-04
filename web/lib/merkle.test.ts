import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { bytesToHex, hexToBytes, verifyInclusion, verifyProofAgainstRoot } from "./merkle";

// The SAME fixture the Python suite reproduces (tests/audit/merkle_vectors.json).
// Reading it here is the cross-language anti-drift guarantee: if merkle.py and
// merkle.ts ever disagree on the RFC-6962 walk, the real Python-generated proofs
// stop verifying in TS and this suite fails.
interface ProofVector {
  m: number;
  leaf_hash_hex: string;
  audit_path: string[];
}
interface TreeVector {
  n: number;
  leaves_hex: string[];
  root_hex: string;
  proofs: ProofVector[];
}
interface Fixture {
  trees: TreeVector[];
}

// Resolve the fixture from cwd — vitest runs with cwd = web/ (config root),
// but tolerate a repo-root cwd too so the suite is invocation-agnostic.
function fixturePath(): string {
  const candidates = [
    resolve(process.cwd(), "../tests/audit/merkle_vectors.json"),
    resolve(process.cwd(), "tests/audit/merkle_vectors.json"),
  ];
  const found = candidates.find((p) => existsSync(p));
  if (!found) {
    throw new Error(
      `merkle_vectors.json not found (looked in: ${candidates.join(", ")}) — ` +
        "regenerate with scripts/gen_merkle_vectors.py",
    );
  }
  return found;
}

const FIXTURE: Fixture = JSON.parse(readFileSync(fixturePath(), "utf-8")) as Fixture;

const tree = (n: number): TreeVector => {
  const t = FIXTURE.trees.find((x) => x.n === n);
  if (!t) throw new Error(`fixture missing tree n=${n}`);
  return t;
};

describe("hex helpers", () => {
  it("round-trips bytes <-> lowercase hex", () => {
    const bytes = new Uint8Array([0x00, 0x0a, 0xff, 0x10]);
    expect(bytesToHex(bytes)).toBe("000aff10");
    expect(hexToBytes("000aff10")).toEqual(bytes);
  });

  it("emits lowercase matching Python bytes.hex()", () => {
    expect(bytesToHex(new Uint8Array([0xab, 0xcd]))).toBe("abcd");
  });

  it("rejects odd-length hex", () => {
    expect(() => hexToBytes("abc")).toThrow();
  });

  it("rejects non-lowercase-hex", () => {
    expect(() => hexToBytes("ABCD")).toThrow();
    expect(() => hexToBytes("zz")).toThrow();
  });

  it("decodes empty string to empty bytes", () => {
    expect(hexToBytes("")).toEqual(new Uint8Array(0));
  });
});

describe("verifyInclusion — cross-language fixture", () => {
  for (const t of FIXTURE.trees.filter((x) => x.n >= 1)) {
    for (const p of t.proofs) {
      it(`accepts the real proof for n=${t.n} m=${p.m}`, async () => {
        const ok = await verifyInclusion(
          p.m,
          t.n,
          hexToBytes(p.leaf_hash_hex),
          p.audit_path.map(hexToBytes),
          hexToBytes(t.root_hex),
        );
        expect(ok).toBe(true);
      });
    }
  }

  it("verifies a single-leaf tree with an empty path (leaf is the root)", async () => {
    const t = tree(1);
    const p = t.proofs[0];
    expect(p.audit_path).toEqual([]);
    expect(p.leaf_hash_hex).toBe(t.root_hex);
    const ok = await verifyInclusion(0, 1, hexToBytes(p.leaf_hash_hex), [], hexToBytes(t.root_hex));
    expect(ok).toBe(true);
  });
});

describe("verifyInclusion — rejections", () => {
  const t = tree(7);
  const p = t.proofs[3];

  it("rejects an out-of-range index", async () => {
    expect(
      await verifyInclusion(
        7,
        7,
        hexToBytes(p.leaf_hash_hex),
        p.audit_path.map(hexToBytes),
        hexToBytes(t.root_hex),
      ),
    ).toBe(false);
    expect(
      await verifyInclusion(
        -1,
        7,
        hexToBytes(p.leaf_hash_hex),
        p.audit_path.map(hexToBytes),
        hexToBytes(t.root_hex),
      ),
    ).toBe(false);
  });

  it("rejects tree_size 0", async () => {
    const empty = tree(0);
    expect(await verifyInclusion(0, 0, hexToBytes(empty.root_hex), [], hexToBytes(empty.root_hex))).toBe(
      false,
    );
  });

  it("rejects a corrupted leaf hash", async () => {
    const leaf = hexToBytes(p.leaf_hash_hex);
    leaf[0] ^= 0x01;
    expect(
      await verifyInclusion(p.m, 7, leaf, p.audit_path.map(hexToBytes), hexToBytes(t.root_hex)),
    ).toBe(false);
  });

  it("rejects a corrupted path entry", async () => {
    const path = p.audit_path.map(hexToBytes);
    path[0][0] ^= 0x01;
    expect(await verifyInclusion(p.m, 7, hexToBytes(p.leaf_hash_hex), path, hexToBytes(t.root_hex))).toBe(
      false,
    );
  });

  it("rejects the wrong root (a different tree)", async () => {
    const wrongRoot = hexToBytes(tree(8).root_hex);
    expect(
      await verifyInclusion(p.m, 7, hexToBytes(p.leaf_hash_hex), p.audit_path.map(hexToBytes), wrongRoot),
    ).toBe(false);
  });

  it("rejects a truncated path", async () => {
    const path = p.audit_path.slice(0, -1).map(hexToBytes);
    expect(await verifyInclusion(p.m, 7, hexToBytes(p.leaf_hash_hex), path, hexToBytes(t.root_hex))).toBe(
      false,
    );
  });

  it("rejects a wrong-length leaf hash", async () => {
    expect(
      await verifyInclusion(
        p.m,
        7,
        new Uint8Array(16),
        p.audit_path.map(hexToBytes),
        hexToBytes(t.root_hex),
      ),
    ).toBe(false);
  });

  it("rejects a wrong-length root", async () => {
    expect(
      await verifyInclusion(p.m, 7, hexToBytes(p.leaf_hash_hex), p.audit_path.map(hexToBytes), new Uint8Array(16)),
    ).toBe(false);
  });

  it("rejects a wrong-length sibling in the path", async () => {
    const path = p.audit_path.map(hexToBytes);
    path[0] = new Uint8Array(16);
    expect(await verifyInclusion(p.m, 7, hexToBytes(p.leaf_hash_hex), path, hexToBytes(t.root_hex))).toBe(
      false,
    );
  });
});

describe("verifyProofAgainstRoot — the honest anchor", () => {
  const t = tree(5);
  const p = t.proofs[2];
  const proof = {
    leaf_index: p.m,
    tree_size: t.n,
    leaf_hash_hex: p.leaf_hash_hex,
    audit_path: p.audit_path,
  };

  it("accepts when the proof reconciles to the supplied global root", async () => {
    expect(await verifyProofAgainstRoot(proof, t.root_hex)).toBe(true);
  });

  it("rejects when the supplied global root is a different tree's root", async () => {
    // The browser passes the SEPARATELY-fetched global root here; a proof
    // rooted at another tree must not pass.
    expect(await verifyProofAgainstRoot(proof, tree(7).root_hex)).toBe(false);
  });
});
