import { DataDictionary } from "@/components/dict/DataDictionary";

/**
 * Data Dictionary surface (/dict), mounted inside the app shell (the shell owns
 * the chrome: top bar, nav rail, source selector, theme — see (app)/layout.tsx).
 *
 * THE DICTIONARY — a generated, browsable view of the curated semantic layer:
 * every entity's description, columns, semantic joins, and PII flags, with a
 * one-click Markdown export that matches the `schemabrain docs` CLI byte for
 * byte. Source-scoped and read-only.
 */
export default function DictPage() {
  return <DataDictionary />;
}
