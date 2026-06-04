"use client";

import { useEffect, useState } from "react";
import { Icon } from "@/components/kit";
import { cn } from "@/lib/cn";
import {
  isEmptyPolicyFilter,
  type PolicyFilter,
  type PolicyStatusFilter,
  prettyCategory,
} from "@/lib/policy";
import { type PIICategory } from "@/lib/types";
import styles from "./ledger.module.css";

/**
 * MatrixToolbar — the controlled filter for the PII heatmap: free-text
 * search, a category select, and an All / Blocked / Redact / Allow status
 * segmented control, plus a PII-only ↔ show-all toggle and a debounced live
 * region announcing the result count. Pure-controlled (owns no filter state).
 * Mirrors the /policy PolicySearchBar's interaction + a11y so the two trust
 * surfaces feel identical, with the matrix's own block/redact/allow vocabulary.
 */
const STATUS_OPTIONS: readonly {
  value: PolicyStatusFilter | null;
  label: string;
  aria: string;
}[] = [
  { value: null, label: "all", aria: "show all statuses" },
  { value: "block", label: "blocked", aria: "filter to blocked, floor-locked, columns" },
  { value: "redact", label: "redact", aria: "filter to redacted columns" },
  { value: "allow", label: "allow", aria: "filter to allowed columns" },
];

const ANNOUNCE_DEBOUNCE_MS = 300;

function resultCountText(active: boolean, columns: number): string {
  if (!active) return "";
  if (columns === 0) return "no columns match";
  return `${columns} ${columns === 1 ? "column" : "columns"}`;
}

export function MatrixToolbar({
  filter,
  categories,
  resultColumns,
  onFilterChange,
  showAll,
  nonPiiCount,
  onToggleShowAll,
}: {
  filter: PolicyFilter;
  categories: readonly PIICategory[];
  resultColumns: number;
  onFilterChange: (filter: PolicyFilter) => void;
  showAll: boolean;
  nonPiiCount: number;
  onToggleShowAll: () => void;
}) {
  const active = !isEmptyPolicyFilter(filter);

  // Debounce the announced count so a screen reader hears the settled result
  // ("3 columns"), not every interim keystroke count (WCAG 4.1.3).
  const [announced, setAnnounced] = useState("");
  useEffect(() => {
    const text = resultCountText(active, resultColumns);
    const id = setTimeout(() => setAnnounced(text), ANNOUNCE_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [active, resultColumns]);

  return (
    <div className={styles.toolbar} role="search">
      <div className={styles.searchField}>
        <Icon name="search" size={14} className={styles.searchIcon} />
        <input
          type="search"
          className={styles.searchInput}
          placeholder="filter columns…"
          aria-label="filter columns"
          value={filter.query}
          onChange={(event) => onFilterChange({ ...filter, query: event.target.value })}
        />
      </div>

      <select
        className={styles.catSelect}
        aria-label="filter by category"
        value={filter.category ?? ""}
        onChange={(event) =>
          onFilterChange({
            ...filter,
            category: event.target.value === "" ? null : (event.target.value as PIICategory),
          })
        }
      >
        <option value="">all categories</option>
        {categories.map((category) => (
          <option key={category} value={category}>
            {prettyCategory(category)}
          </option>
        ))}
      </select>

      <div className={styles.statusSeg} role="group" aria-label="filter by status">
        {STATUS_OPTIONS.map((option) => {
          const selected = filter.status === option.value;
          return (
            <button
              key={option.label}
              type="button"
              aria-pressed={selected}
              aria-label={option.aria}
              className={cn(selected && styles.statusOn)}
              onClick={() => onFilterChange({ ...filter, status: option.value })}
            >
              {option.label}
            </button>
          );
        })}
      </div>

      {active && (
        <button
          type="button"
          className={styles.toolbarBtn}
          onClick={() => onFilterChange({ query: "", category: null, status: null })}
        >
          <Icon name="x" size={13} /> clear
        </button>
      )}

      {nonPiiCount > 0 && (
        <button
          type="button"
          className={styles.toolbarBtn}
          aria-pressed={showAll}
          onClick={onToggleShowAll}
        >
          <Icon name={showAll ? "eye-off" : "boxes"} size={13} />{" "}
          {showAll ? "PII only" : `show all (+${nonPiiCount})`}
        </button>
      )}

      <p className={styles.resultCount} role="status" aria-live="polite">
        {announced}
      </p>
    </div>
  );
}
