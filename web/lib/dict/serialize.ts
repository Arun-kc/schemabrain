// Faithful TypeScript port of schemabrain/datadict/render_markdown.py.
//
// Renders a `DictionaryModel` (GET /api/dict) to the IDENTICAL
// GitHub-flavoured, Mintlify-safe Markdown the `schemabrain docs` CLI
// emits, so the dashboard's "Export Markdown" is byte-for-byte equal to
// the committed CLI golden. `serialize.test.ts` asserts that equality
// against tests/datadict/golden/saas_dictionary.md — editing either
// serialiser (Python or this) breaks it.
//
// Mintlify-safety rules preserved from the Python source:
//   - no raw `|` inside a table cell (multi-value cells use ` / `
//     separators; free-text cells escape `|` → `\|`);
//   - literal `$` is backslash-escaped (dodges MDX inline-math);
//   - identifiers/types/ON-clauses render inside single backtick spans;
//   - exactly one H1 per page; one trailing newline.

import type {
  DictColumn,
  DictEntity,
  DictionaryModel,
  DictJoin,
  DictMetric,
  DictSource,
} from "@/lib/types";
import { categoryLabel, isCatastrophicCategory, sensitivityLabel } from "@/lib/dict/labels";

const EM_DASH = "—";

const COLUMN_HEADER =
  "| Column | Type | Null | PK | Identity | Sensitivity | PII categories | Description |";
const COLUMN_DIVIDER = "| --- | --- | --- | --- | --- | --- | --- | --- |";
const JOIN_HEADER = "| Join | On | Cardinality | Provenance | Description |";
const JOIN_DIVIDER = "| --- | --- | --- | --- | --- |";
const METRIC_HEADER = "| Metric | Aggregation | Measure | Time dimension | Grains | Description |";
const METRIC_DIVIDER = "| --- | --- | --- | --- | --- | --- |";

function yesNo(value: boolean): string {
  return value ? "yes" : "no";
}

/** Escape a literal `$` in prose so MDX doesn't read it as math. */
function escapeProse(text: string): string {
  return text.replaceAll("$", "\\$");
}

/** Escape free text for a Markdown table cell (no raw pipe / newline / $). */
function cell(text: string): string {
  return text.replaceAll("\n", " ").replaceAll("|", "\\|").replaceAll("$", "\\$");
}

/** A free-text cell, or the em-dash when empty/absent. */
function descCell(text: string | null): string {
  return text ? cell(text) : EM_DASH;
}

/** A backtick code span for a single non-pipe identifier/type/clause. */
function code(value: string): string {
  return `\`${value}\``;
}

function piiCell(categories: readonly string[]): string {
  if (categories.length === 0) return EM_DASH;
  return categories
    .map((category) => {
      let piece = code(categoryLabel(category as never));
      if (isCatastrophicCategory(category as never)) piece += " (catastrophic)";
      return piece;
    })
    .join(" / ");
}

function columnsTable(columns: readonly DictColumn[], heading: string): string[] {
  const lines = ["", `${heading} Columns`, "", COLUMN_HEADER, COLUMN_DIVIDER];
  for (const col of columns) {
    lines.push(
      `| ${code(col.name)} | ${code(col.data_type)} | ${yesNo(col.nullable)} | ` +
        `${yesNo(col.is_primary_key)} | ${yesNo(col.is_identity)} | ` +
        `${sensitivityLabel(col.pii_sensitivity)} | ${piiCell(col.pii_categories)} | ` +
        `${descCell(col.description)} |`,
    );
  }
  return lines;
}

function joinsTable(joins: readonly DictJoin[], heading: string): string[] {
  const lines = ["", `${heading} Joins`, "", JOIN_HEADER, JOIN_DIVIDER];
  for (const join of joins) {
    const cardinality = join.cardinality ? join.cardinality : EM_DASH;
    lines.push(
      `| ${code(join.name)} | ${code(join.on_clause)} | ${cardinality} | ` +
        `${join.provenance} | ${descCell(join.description)} |`,
    );
  }
  return lines;
}

function metricsTable(metrics: readonly DictMetric[], heading: string): string[] {
  const lines = ["", `${heading} Metrics`, "", METRIC_HEADER, METRIC_DIVIDER];
  for (const metric of metrics) {
    const timeDimension = metric.time_dimension ? code(metric.time_dimension) : EM_DASH;
    const grains = metric.time_grains.length > 0 ? metric.time_grains.join(", ") : EM_DASH;
    lines.push(
      `| ${code(metric.name)} | ${metric.agg} | ${code(metric.measure)} | ` +
        `${timeDimension} | ${grains} | ${descCell(metric.description)} |`,
    );
  }
  return lines;
}

function entityBlock(entity: DictEntity, level: number): string[] {
  const entityPrefix = "#".repeat(level);
  const subPrefix = "#".repeat(level + 1);
  const lines = [
    "",
    `${entityPrefix} ${entity.name}`,
    "",
    entity.description ? escapeProse(entity.description) : EM_DASH,
    "",
    `- **Table:** ${code(entity.qualified_table)}`,
    `- **Identity:** ${code(entity.identity)}`,
    `- **Group:** ${entity.group}`,
  ];
  lines.push(...columnsTable(entity.columns, subPrefix));
  if (entity.joins.length > 0) lines.push(...joinsTable(entity.joins, subPrefix));
  if (entity.metrics.length > 0) lines.push(...metricsTable(entity.metrics, subPrefix));
  return lines;
}

/**
 * Render the full dictionary model as a single Markdown document — the
 * byte-for-byte twin of `render_markdown(model)` in Python.
 */
export function serializeDictionary(model: DictionaryModel): string {
  const lines: string[] = [
    "# Data dictionary",
    "",
    `Generated from the local SchemaBrain store (schema version ${model.schema_version}). ` +
      "Every indexed table, column, type, PII classification, semantic join, and metric.",
  ];
  const multiSource = model.sources.length > 1;
  // One source → entities head the page at ##. Several → each source gets a
  // ## divider and its entities nest at ###.
  const entityLevel = multiSource ? 3 : 2;
  for (const source of model.sources as readonly DictSource[]) {
    if (multiSource) lines.push("", `## Source: ${code(source.source_connection_id)}`);
    for (const entity of source.entities) lines.push(...entityBlock(entity, entityLevel));
  }
  return lines.join("\n") + "\n";
}
