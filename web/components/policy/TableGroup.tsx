"use client";

import { Icon } from "@/components/kit";
import { cn } from "@/lib/cn";
import {
  columnVerb,
  countVerbs,
  type PolicyVerb,
  siblingsAffectedByVerb,
  type StagedOverrides,
} from "@/lib/policy";
import { type PIICategory, type PolicyColumnEntry } from "@/lib/types";
import { PerColumnRow } from "./PerColumnRow";
import styles from "./policy-editor.module.css";

/**
 * TableGroup — a collapsible disclosure for one table's columns. The header
 * is a `<button aria-expanded aria-controls>` showing the qualified table, the
 * (filtered) column count, and a mini status summary so operators can spot the
 * "hot" tables while everything is collapsed. The panel nests `PerColumnRow`s.
 *
 * CRITICAL (ADR 0008 §3): `siblingsAffectedByVerb` is computed over the FULL
 * per-column listing (`fullPerColumn`), never `columns` (which may be a
 * filtered subset) — a search filter must not change a verb's disclosed blast
 * radius. Rows mount only while expanded (collapse-by-default is the scaling
 * win); the panel element stays in the DOM as a valid `aria-controls` target.
 */
export function TableGroup({
  qualifiedTable,
  columns,
  fullPerColumn,
  stagedBlock,
  stagedOverrides,
  initialBlock,
  initialOverrides,
  expanded,
  panelId,
  onToggle,
  onSetVerb,
}: {
  qualifiedTable: string;
  columns: readonly PolicyColumnEntry[];
  fullPerColumn: readonly PolicyColumnEntry[];
  stagedBlock: ReadonlySet<PIICategory>;
  stagedOverrides: StagedOverrides;
  initialBlock: ReadonlySet<PIICategory>;
  initialOverrides: StagedOverrides;
  expanded: boolean;
  panelId: string;
  onToggle: () => void;
  onSetVerb: (row: PolicyColumnEntry, verb: PolicyVerb) => void;
}) {
  // Header count + mini-summary describe THIS group's `columns`. With no
  // filter that's the whole table (the common "scan the hot tables while
  // collapsed" case → accurate); under an active filter the group holds only
  // the matched columns and auto-expands, so the header honestly describes the
  // filtered subset. (Distinct from the sibling math below, which is full-set.)
  const counts = countVerbs(columns, stagedBlock, stagedOverrides);

  return (
    <div className={styles.group}>
      <button
        type="button"
        className={styles.groupHead}
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <Icon
          name="chevron-down"
          size={14}
          className={cn(styles.groupChevron, !expanded && styles.groupChevronClosed)}
        />
        <Icon name="table-2" size={13} className={styles.groupTableIcon} />
        <span className={styles.groupName}>{qualifiedTable}</span>
        <span className={styles.groupCount}>
          {columns.length} {columns.length === 1 ? "column" : "columns"}
        </span>
        <span className={styles.groupMini}>
          {counts.block > 0 && <span className={styles.miniBlock}>{counts.block} blocked</span>}
          {counts.floor > 0 && <span className={styles.miniFloor}>{counts.floor} floor</span>}
          {counts.allow > 0 && <span className={styles.miniAllow}>{counts.allow} allowed</span>}
          {counts.redact > 0 && <span className={styles.miniRedact}>{counts.redact} open</span>}
        </span>
      </button>
      <div
        id={panelId}
        role="group"
        aria-label={qualifiedTable}
        className={styles.groupPanel}
        hidden={!expanded}
      >
        {expanded &&
          columns.map((row) => {
            const verb = columnVerb(
              row.qualified_column,
              row.categories,
              stagedBlock,
              stagedOverrides,
            );
            const initialVerb = columnVerb(
              row.qualified_column,
              row.categories,
              initialBlock,
              initialOverrides,
            );
            // Disclosure blast radius over the FULL set (constraint #3). `allow`
            // + `floor` never move siblings → 0.
            const siblings =
              verb === "floor"
                ? 0
                : siblingsAffectedByVerb(
                    fullPerColumn,
                    row.qualified_column,
                    row.categories,
                    verb,
                    stagedBlock,
                    stagedOverrides,
                  );
            return (
              <PerColumnRow
                key={row.qualified_column}
                row={row}
                verb={verb}
                pending={verb !== initialVerb}
                siblings={siblings}
                onSetVerb={onSetVerb}
              />
            );
          })}
      </div>
    </div>
  );
}
