"use client";

import { useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/kit";
import {
  filterPerColumn,
  groupByTable,
  isEmptyPolicyFilter,
  piiColumns,
  type PolicyVerb,
  type StagedOverrides,
  summarizePolicyCategories,
} from "@/lib/policy";
import { usePolicyFilter } from "@/lib/usePolicyFilter";
import { type PIICategory, type PolicyColumnEntry } from "@/lib/types";
import { CategorySummaryStrip } from "./CategorySummaryStrip";
import { PolicySearchBar } from "./PolicySearchBar";
import { TableGroup } from "./TableGroup";
import styles from "./policy-editor.module.css";

/**
 * PerColumnPane — the scalable left pane. It filters the full per-column
 * listing, groups the result by table into collapsible `TableGroup`s, and
 * renders the full-set category strip + search bar above them. This is what
 * lets the page hold up on a real multi-table schema: collapse-by-default
 * means only expanded groups mount their rows.
 *
 * The category strip + the sibling-disclosure math are always computed over
 * the FULL `perColumn` (never the filtered subset) — ADR 0008 §3.
 *
 * Must be rendered inside a `<Suspense>` boundary (it uses usePolicyFilter →
 * useSearchParams, which requires one under `output: "export"`).
 */
export function PerColumnPane({
  perColumn,
  stagedBlock,
  stagedOverrides,
  initialBlock,
  initialOverrides,
  onSetVerb,
  onToggleCategory,
}: {
  perColumn: readonly PolicyColumnEntry[];
  stagedBlock: ReadonlySet<PIICategory>;
  stagedOverrides: StagedOverrides;
  initialBlock: ReadonlySet<PIICategory>;
  initialOverrides: StagedOverrides;
  onSetVerb: (row: PolicyColumnEntry, verb: PolicyVerb) => void;
  onToggleCategory: (category: PIICategory, block: boolean) => void;
}) {
  const { filter, setFilter } = usePolicyFilter();
  const filterActive = !isEmptyPolicyFilter(filter);

  // No-category columns have no meaningful 3-way action (block needs a
  // category; allow on a non-PII column is a no-op), so the grid shows only
  // PII-bearing columns by default and offers a toggle to reveal the rest.
  const [showAll, setShowAll] = useState(false);
  const pii = useMemo(() => piiColumns(perColumn), [perColumn]);
  const nonPiiCount = perColumn.length - pii.length;
  const baseColumns = showAll ? perColumn : pii;

  const filtered = useMemo(
    () => filterPerColumn(baseColumns, filter, stagedBlock, stagedOverrides),
    [baseColumns, filter, stagedBlock, stagedOverrides],
  );
  const groups = useMemo(() => groupByTable(filtered), [filtered]);
  const summaries = useMemo(
    () => summarizePolicyCategories(perColumn, stagedBlock, stagedOverrides),
    [perColumn, stagedBlock, stagedOverrides],
  );
  const categories = useMemo(() => summaries.map((s) => s.category), [summaries]);

  const singleTable = groups.length === 1;
  // Signature of the matching tables — drives the auto-expand effect so it
  // re-runs when a search narrows/widens the visible set. JSON (not
  // join("|")) so a table name containing a literal "|" (a quoted Postgres
  // identifier) round-trips exactly rather than splitting into bad keys.
  const groupSignature = useMemo(
    () => JSON.stringify(groups.map((g) => g.qualifiedTable)),
    [groups],
  );

  // Expanded-set state. Default: all collapsed (the scaling win). Auto-expand
  // when (a) a single table is shown, or (b) a filter is active (results must
  // be visible). The effect reconciles those modes; user toggles persist while
  // the mode (and matching set) is stable. See plan §E/§H.
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    if (singleTable || filterActive) {
      setExpanded(new Set(JSON.parse(groupSignature) as string[]));
    } else {
      setExpanded(new Set());
    }
  }, [singleTable, filterActive, groupSignature]);

  const toggle = (table: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(table)) next.delete(table);
      else next.add(table);
      return next;
    });
  };

  return (
    <section className={styles.pane} aria-label="per-column enforcement">
      <div className={styles.paneHead}>
        <Icon name="sliders-horizontal" size={14} /> Per-column enforcement
        <span className={styles.paneCount}>
          {baseColumns.length} {baseColumns.length === 1 ? "column" : "columns"}
          {!showAll && nonPiiCount > 0 ? " · PII only" : ""}
        </span>
      </div>

      {perColumn.length === 0 ? (
        <p className={styles.empty}>no columns have been tagged on this source yet</p>
      ) : (
        <>
          <CategorySummaryStrip summaries={summaries} onToggleCategory={onToggleCategory} />
          <PolicySearchBar
            filter={filter}
            categories={categories}
            resultColumns={filtered.length}
            resultTables={groups.length}
            onFilterChange={setFilter}
            showAll={showAll}
            nonPiiCount={nonPiiCount}
            onToggleShowAll={() => setShowAll((v) => !v)}
          />
          {groups.length === 0 ? (
            filterActive ? (
              <p className={styles.empty}>
                no columns match —{" "}
                <button
                  type="button"
                  className={styles.linkBtn}
                  onClick={() => setFilter({ query: "", category: null, status: null })}
                >
                  clear the filter
                </button>
              </p>
            ) : (
              <p className={styles.empty}>
                no PII-tagged columns on this source.
                {nonPiiCount > 0 && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className={styles.linkBtn}
                      onClick={() => setShowAll(true)}
                    >
                      show all {perColumn.length} columns
                    </button>
                  </>
                )}
              </p>
            )
          ) : (
            <div className={styles.groupList}>
              {groups.map((group) => (
                <TableGroup
                  key={group.qualifiedTable}
                  qualifiedTable={group.qualifiedTable}
                  columns={group.columns}
                  fullPerColumn={perColumn}
                  stagedBlock={stagedBlock}
                  stagedOverrides={stagedOverrides}
                  initialBlock={initialBlock}
                  initialOverrides={initialOverrides}
                  expanded={expanded.has(group.qualifiedTable)}
                  panelId={`policy-group-${group.qualifiedTable.replace(/[^a-zA-Z0-9]/g, "-")}`}
                  onToggle={() => toggle(group.qualifiedTable)}
                  onSetVerb={onSetVerb}
                />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}
