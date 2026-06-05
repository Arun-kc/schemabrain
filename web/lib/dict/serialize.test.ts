import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { DictionaryModel } from "@/lib/types";
import { serializeDictionary } from "@/lib/dict/serialize";

// Cross-language parity. The Python CLI golden (saas_dictionary.md) is the
// ONE source of truth; the committed wire-shape fixture (saas_dictionary.
// model.json, pinned to the live aggregator by the Python
// `test_model_json_matches_committed_fixture`) is the bridge. This asserts
// the TS serialiser renders that model byte-for-byte equal to the golden —
// so editing either serialiser, or drifting a label/escape rule, fails here.
// vitest runs from `web/`, so the repo-root golden lives one level up.
const GOLDEN_DIR = resolve(process.cwd(), "..", "tests", "datadict", "golden");

function readGoldenFile(name: string): string {
  return readFileSync(resolve(GOLDEN_DIR, name), "utf-8");
}

const MODEL = JSON.parse(readGoldenFile("saas_dictionary.model.json")) as DictionaryModel;
const GOLDEN_MD = readGoldenFile("saas_dictionary.md");

describe("serializeDictionary", () => {
  it("renders the committed model byte-for-byte equal to the CLI golden", () => {
    expect(serializeDictionary(MODEL)).toBe(GOLDEN_MD);
  });

  it("emits exactly one H1 and a single trailing newline", () => {
    const out = serializeDictionary(MODEL);
    const h1s = out.split("\n").filter((line) => line.startsWith("# "));
    expect(h1s).toEqual(["# Data dictionary"]);
    expect(out.endsWith("\n")).toBe(true);
    expect(out.endsWith("\n\n")).toBe(false);
  });

  it("renders an empty model as just the header", () => {
    const empty: DictionaryModel = { schema_version: "17", sources: [] };
    expect(serializeDictionary(empty)).toBe(
      "# Data dictionary\n\nGenerated from the local SchemaBrain store " +
        "(schema version 17). Every indexed table, column, type, PII " +
        "classification, semantic join, and metric.\n",
    );
  });
});
