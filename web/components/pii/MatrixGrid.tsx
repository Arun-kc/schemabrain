"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { Icon, PiiChip, type PiiChipKind } from "@/components/kit";
import { cn } from "@/lib/cn";
import { isEmptyPolicyFilter } from "@/lib/policy";
import { usePolicyFilter } from "@/lib/usePolicyFilter";
import { type PIICategory, type PiiMatrixColumn } from "@/lib/types";
import {
  type CellState,
  cellAriaLabel,
  cellState,
  columnStatus,
  filterMatrixColumns,
  isFloorColumn,
  isPiiColumn,
  type MatrixStatus,
  qualifiedColumn,
  sortFloorFirst,
  statusLabel,
} from "@/lib/piiMatrix";
import { MatrixToolbar } from "./MatrixToolbar";
import styles from "./ledger.module.css";

/**
 * MatrixGrid — the PII confidence heatmap (THE READOUT direction): one row
 * per classified column, 12 fixed category columns, a sticky column channel
 * on the left and a sticky Status channel on the right, with the 12 category
 * cells scrolling between them. Floor columns are pinned to the top in an
 * alarm band. Cell intensity is the column's single advisory band (ADR 0009),
 * applied to its member cells only — never a fabricated per-category score.
 *
 * Owns the filter (URL-backed via usePolicyFilter → useSearchParams, so it
 * MUST be rendered inside a <Suspense> boundary under `output: "export"`) and
 * the PII-only ↔ show-all toggle; selection is lifted to the parent so the
 * entity Drilldown can sit outside the Suspense boundary.
 */
const STATUS_CHIP: Record<MatrixStatus, PiiChipKind> = {
  block: "payment",
  redact: "cyan",
  allow: "green",
};

const DOT_CLASS: Record<"floor" | "high" | "medium" | "low", string> = {
  floor: styles.dotFloor,
  high: styles.dotHigh,
  medium: styles.dotMedium,
  low: styles.dotLow,
};

const CATEGORY_ABBR: Record<string, string> = {
  contact: "cont",
  financial: "fin",
  payment_card: "pmt",
  health: "hlth",
  genetic: "gen",
  biometric: "bio",
  behavioral: "bhv",
  online_identifier: "oid",
  credential: "cred",
  government_id: "gov",
  location: "loc",
  demographic_protected: "dem",
};

function abbreviate(category: string): string {
  return CATEGORY_ABBR[category] ?? category.slice(0, 4);
}

export function MatrixGrid({
  columns,
  categories,
  catastrophicCategories,
  selectedColumn,
  onSelect,
}: {
  columns: readonly PiiMatrixColumn[];
  categories: readonly PIICategory[];
  catastrophicCategories: readonly string[];
  selectedColumn: string | null;
  onSelect: (col: PiiMatrixColumn) => void;
}) {
  const { filter, setFilter } = usePolicyFilter();
  const filterActive = !isEmptyPolicyFilter(filter);
  const [showAll, setShowAll] = useState(false);

  // `?focus=<entity>` arrives from the entity sheet's "View in PII matrix"
  // deep-link — scroll the entity's first rendered row into view + flash it.
  // Read-only and best-effort: if the entity's columns are filtered out (or
  // non-PII while the PII-only view is on), nothing scrolls rather than
  // fabricating a target.
  const focusEntity = useSearchParams().get("focus");

  const pii = useMemo(() => columns.filter(isPiiColumn), [columns]);
  const nonPiiCount = columns.length - pii.length;
  const base = showAll ? columns : pii;

  const filtered = useMemo(() => filterMatrixColumns(base, filter), [base, filter]);
  const rows = useMemo(() => sortFloorFirst(filtered), [filtered]);

  const focusKey = useMemo(() => {
    if (!focusEntity) return null;
    const match = rows.find((col) => col.entity === focusEntity);
    return match ? qualifiedColumn(match) : null;
  }, [focusEntity, rows]);

  const catSet = useMemo(() => new Set(catastrophicCategories), [catastrophicCategories]);
  const presentCategories = useMemo(
    () => categories.filter((cat) => columns.some((col) => col.categories.includes(cat))),
    [categories, columns],
  );

  return (
    <section className={styles.gridSection} aria-label="pii confidence heatmap">
      <MatrixToolbar
        filter={filter}
        categories={presentCategories}
        resultColumns={filtered.length}
        onFilterChange={setFilter}
        showAll={showAll}
        nonPiiCount={nonPiiCount}
        onToggleShowAll={() => setShowAll((v) => !v)}
      />

      {rows.length === 0 ? (
        <MatrixEmpty
          filterActive={filterActive}
          showAll={showAll}
          nonPiiCount={nonPiiCount}
          onClear={() => setFilter({ query: "", category: null, status: null })}
          onShowAll={() => setShowAll(true)}
        />
      ) : (
        <div className={styles.matrixWrap}>
          <table className={styles.matrix} aria-label="pii matrix">
            <thead>
              <tr className={styles.headRow}>
                <th scope="col" className={cn(styles.thColumn, styles.stickyLeft)}>
                  column
                </th>
                {categories.map((cat) => {
                  const floor = catSet.has(cat);
                  return (
                    <th
                      key={cat}
                      scope="col"
                      className={cn(styles.catHeader, floor && styles.catHeaderFloor)}
                      aria-label={
                        floor
                          ? `${cat.replace(/_/g, " ")} — catastrophic floor, blocked for every agent`
                          : cat.replace(/_/g, " ")
                      }
                      title={cat.replace(/_/g, " ")}
                    >
                      {floor && (
                        <Icon name="lock" size={10} className={styles.catLock} />
                      )}
                      <span className={styles.catHeaderLabel}>{abbreviate(cat)}</span>
                    </th>
                  );
                })}
                <th scope="col" className={cn(styles.statHeader, styles.stickyRight)}>
                  status
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((col) => (
                <ColumnRow
                  key={qualifiedColumn(col)}
                  col={col}
                  categories={categories}
                  selected={selectedColumn === qualifiedColumn(col)}
                  focused={focusKey !== null && qualifiedColumn(col) === focusKey}
                  onSelect={() => onSelect(col)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Legend />
    </section>
  );
}

function ColumnRow({
  col,
  categories,
  selected,
  focused,
  onSelect,
}: {
  col: PiiMatrixColumn;
  categories: readonly PIICategory[];
  selected: boolean;
  focused: boolean;
  onSelect: () => void;
}) {
  const floor = isFloorColumn(col);
  const status = columnStatus(col);
  const ref = useRef<HTMLTableRowElement>(null);

  useEffect(() => {
    if (focused && ref.current) {
      ref.current.scrollIntoView({ block: "center" });
    }
  }, [focused]);

  return (
    <tr
      ref={ref}
      className={cn(floor && styles.floorRow, selected && styles.selectedRow, focused && styles.focusFlash)}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      tabIndex={0}
      aria-selected={selected}
    >
      <td className={cn(styles.tdColumn, styles.stickyLeft)}>
        {floor && <Icon name="lock" size={12} className={styles.tdLock} label="floor-locked" />}
        <span className={styles.tdColName}>{col.column_name}</span>
        <span className={styles.tdColTable}>{col.qualified_table}</span>
      </td>
      {categories.map((category) => (
        <Cell key={category} col={col} category={category} />
      ))}
      <td className={cn(styles.statCell, styles.stickyRight)}>
        <PiiChip kind={STATUS_CHIP[status]} dot className={styles.statChip}>
          {statusLabel(status)}
        </PiiChip>
      </td>
    </tr>
  );
}

function Cell({ col, category }: { col: PiiMatrixColumn; category: PIICategory }) {
  const state: CellState = cellState(col, category);
  if (state === "unlit") {
    return (
      <td className={styles.cell}>
        <span className={styles.unlit} aria-hidden="true">
          ·
        </span>
      </td>
    );
  }
  const label = cellAriaLabel(col, category);
  if (state === "nullband") {
    return (
      <td className={styles.cell} aria-label={label}>
        <span className={styles.nullBand} title={label}>
          —
        </span>
      </td>
    );
  }
  return (
    <td className={styles.cell} aria-label={label}>
      <span className={cn(styles.dot, DOT_CLASS[state])} title={label} />
    </td>
  );
}

function Legend() {
  return (
    <div className={styles.legend}>
      <span className={styles.legendKey}>
        <span className={cn(styles.dot, styles.dotFloor)} aria-hidden="true" /> floor-locked
      </span>
      <span className={styles.legendKey}>
        <span className={cn(styles.dot, styles.dotHigh)} aria-hidden="true" /> high
      </span>
      <span className={styles.legendKey}>
        <span className={cn(styles.dot, styles.dotMedium)} aria-hidden="true" /> medium
      </span>
      <span className={styles.legendKey}>
        <span className={cn(styles.dot, styles.dotLow)} aria-hidden="true" /> low
      </span>
      <span className={styles.legendKey}>
        <span className={styles.nullBand} aria-hidden="true">
          —
        </span>{" "}
        no band
      </span>
      <span className={styles.legendNote}>
        band is advisory · never gates enforcement · row → drill in · edit enforcement in Policy
      </span>
    </div>
  );
}

function MatrixEmpty({
  filterActive,
  showAll,
  nonPiiCount,
  onClear,
  onShowAll,
}: {
  filterActive: boolean;
  showAll: boolean;
  nonPiiCount: number;
  onClear: () => void;
  onShowAll: () => void;
}) {
  if (filterActive) {
    return (
      <p className={styles.gridEmpty}>
        no columns match —{" "}
        <button type="button" className={styles.linkBtn} onClick={onClear}>
          clear the filter
        </button>
      </p>
    );
  }
  return (
    <p className={styles.gridEmpty}>
      no PII-classified columns on this source.
      {!showAll && nonPiiCount > 0 && (
        <>
          {" "}
          <button type="button" className={styles.linkBtn} onClick={onShowAll}>
            show all {nonPiiCount} columns
          </button>
        </>
      )}
    </p>
  );
}
