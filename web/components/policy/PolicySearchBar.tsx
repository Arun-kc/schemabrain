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
import styles from "./policy-editor.module.css";

const STATUS_OPTIONS: readonly {
  value: PolicyStatusFilter | null;
  label: string;
  aria: string;
}[] = [
  { value: null, label: "all", aria: "show all statuses" },
  { value: "block", label: "blocked", aria: "filter to blocked columns" },
  { value: "redact", label: "open", aria: "filter to open, not blocked, columns" },
  { value: "allow", label: "allowed", aria: "filter to allowed columns" },
  { value: "floor", label: "floor", aria: "filter to locked floor columns" },
];

const ANNOUNCE_DEBOUNCE_MS = 300;

function resultCountText(active: boolean, columns: number, tables: number): string {
  if (!active) return "";
  if (columns === 0) return "no columns match";
  return `${columns} ${columns === 1 ? "column" : "columns"} · ${tables} ${
    tables === 1 ? "table" : "tables"
  }`;
}

/**
 * PolicySearchBar — a controlled filter for the per-column grid: free-text
 * search, a category select, and a status segmented control, plus a live
 * region announcing the result count. Pure-controlled: it owns no state, just
 * `filter` + `onFilterChange`. Filtering is what makes the grid usable on a
 * real multi-table schema.
 */
export function PolicySearchBar({
  filter,
  categories,
  resultColumns,
  resultTables,
  onFilterChange,
  showAll,
  nonPiiCount,
  onToggleShowAll,
}: {
  filter: PolicyFilter;
  categories: readonly PIICategory[];
  resultColumns: number;
  resultTables: number;
  onFilterChange: (filter: PolicyFilter) => void;
  showAll: boolean;
  nonPiiCount: number;
  onToggleShowAll: () => void;
}) {
  const active = !isEmptyPolicyFilter(filter);

  // Debounce the announced (and shown) result count: the grid filters
  // instantly, but updating a `aria-live` region on every keystroke makes
  // screen readers read each interim count ("4… 2… 1…"). Announce the settled
  // count instead (WCAG 4.1.3 — status messages should not over-fire).
  const [announced, setAnnounced] = useState("");
  useEffect(() => {
    const text = resultCountText(active, resultColumns, resultTables);
    const id = setTimeout(() => setAnnounced(text), ANNOUNCE_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [active, resultColumns, resultTables]);

  return (
    <div className={styles.searchBar} role="search">
      <div className={styles.searchField}>
        <Icon name="search" size={14} className={styles.searchIcon} />
        <input
          type="search"
          className={styles.searchInput}
          placeholder="Search tables, columns, categories…"
          aria-label="search columns"
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
          className={styles.searchClear}
          onClick={() => onFilterChange({ query: "", category: null, status: null })}
        >
          <Icon name="x" size={13} /> clear
        </button>
      )}

      {/* Reveal/hide the non-PII columns (no enforcement action) — only shown
       * when there are any to reveal. */}
      {nonPiiCount > 0 && (
        <button
          type="button"
          className={styles.searchClear}
          aria-pressed={showAll}
          onClick={onToggleShowAll}
        >
          <Icon name={showAll ? "eye-off" : "boxes"} size={13} />{" "}
          {showAll ? "PII only" : `show all (+${nonPiiCount} non-PII)`}
        </button>
      )}

      {/* Polite live region — announces the settled (debounced) result count. */}
      <p className={styles.searchCount} role="status" aria-live="polite">
        {announced}
      </p>
    </div>
  );
}
