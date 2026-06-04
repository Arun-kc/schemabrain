"use client";

import { type KeyboardEvent, useRef } from "react";
import { Icon } from "@/components/kit";
import { cn } from "@/lib/cn";
import { categoryWideNote, type PolicyVerb, prettyCategory } from "@/lib/policy";
import { type PolicyColumnEntry } from "@/lib/types";
import styles from "./policy-editor.module.css";

/**
 * PerColumnRow — one row of the per-column enforcement grid: the column
 * identity (table · name · categories + category-wide note) and the 3-way
 * `block | redact | allow` control, rendered as a WAI-ARIA radiogroup
 * (role=radio, aria-checked, roving tabindex, arrow-key selection with
 * focus-follows-selection). Floor (catastrophic) columns render a locked tag
 * instead of the control.
 *
 * Extracted verbatim from PolicyEditor so the radiogroup + keyboard contract
 * (and the e2e selectors that depend on it) stay byte-stable while the pane
 * around it grows table grouping / search / a category strip.
 */
const VERBS: readonly PolicyVerb[] = ["block", "redact", "allow"];

const VERB_ACTIVE_CLASS: Record<PolicyVerb, string> = {
  block: "onBlock",
  redact: "onRedact",
  allow: "onAllow",
};

export function PerColumnRow({
  row,
  verb,
  pending,
  siblings,
  onSetVerb,
}: {
  row: PolicyColumnEntry;
  verb: PolicyVerb | "floor";
  pending: boolean;
  siblings: number;
  onSetVerb: (row: PolicyColumnEntry, verb: PolicyVerb) => void;
}) {
  const isFloor = verb === "floor";
  // `block` has no effect on a column with no categories (nothing to add to
  // the block set) — disable that segment and hint at the real fix.
  const noCategories = row.categories.length === 0;
  const isEnabled = (option: PolicyVerb) => !(option === "block" && noCategories);
  const note = isFloor ? null : categoryWideNote(verb as PolicyVerb, siblings);
  const noteId = `pol-note-${row.qualified_column.replace(/[^a-zA-Z0-9]/g, "-")}`;

  // Radio refs so arrow-key navigation moves DOM focus to the newly-selected
  // option (WAI-ARIA radiogroup: focus must follow the checked radio).
  const radioRefs = useRef(new Map<PolicyVerb, HTMLButtonElement>());

  // Roving tabindex + arrow-key selection (WAI-ARIA radiogroup pattern):
  // arrows move to the next/previous ENABLED option, select it, AND move
  // focus to it (the element stays mounted across the re-render).
  const moveSelection = (delta: number) => {
    if (isFloor) return;
    const current = VERBS.indexOf(verb as PolicyVerb);
    for (let step = 1; step <= VERBS.length; step += 1) {
      const candidate = VERBS[(current + delta * step + VERBS.length * step) % VERBS.length];
      if (isEnabled(candidate)) {
        onSetVerb(row, candidate);
        radioRefs.current.get(candidate)?.focus();
        return;
      }
    }
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      moveSelection(1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      moveSelection(-1);
    }
  };

  return (
    <div className={cn(styles.rule, isFloor ? styles.floor : pending && styles.pending)}>
      <div className={styles.ruleId}>
        <div className={styles.ruleTable}>{row.qualified_table}</div>
        <div className={styles.ruleName}>
          {/* decorative — the labelled lock lives on the `floor · locked`
           * tag so the locked state is announced exactly once. */}
          {isFloor && <Icon name="lock" size={12} className={styles.floorLock} />}
          {row.column_name}
        </div>
        <div className={styles.ruleCats}>
          {noCategories ? "no categories" : row.categories.map(prettyCategory).join(" · ")}
          {note && (
            <span id={noteId} className={styles.catWide}>
              {" "}
              · {note}
            </span>
          )}
        </div>
      </div>
      {isFloor ? (
        <span className={styles.floorTag}>
          <Icon name="lock" size={11} label="locked floor" /> floor · locked
        </span>
      ) : (
        <div
          className={styles.seg}
          role="radiogroup"
          aria-label={`enforcement for ${row.qualified_column}`}
          aria-describedby={note ? noteId : undefined}
          onKeyDown={onKeyDown}
        >
          {VERBS.map((option) => {
            const active = verb === option;
            const enabled = isEnabled(option);
            // Fold the disabled rationale into the accessible name — a `title`
            // alone isn't announced by screen readers (and a native-disabled
            // control can't be focused to surface it). The `^block ` prefix is
            // preserved so role/name selectors still match.
            const optionLabel = enabled
              ? `${option} ${row.qualified_column}`
              : `${option} ${row.qualified_column} — unavailable, no category to block`;
            return (
              <button
                key={option}
                ref={(el) => {
                  if (el) radioRefs.current.set(option, el);
                  else radioRefs.current.delete(option);
                }}
                type="button"
                role="radio"
                aria-checked={active}
                aria-label={optionLabel}
                disabled={!enabled}
                title={enabled ? undefined : "no category to block — reclassify via `schemabrain policy tag` first"}
                tabIndex={active ? 0 : -1}
                className={cn(active && styles[VERB_ACTIVE_CLASS[option]])}
                onClick={() => enabled && onSetVerb(row, option)}
              >
                {option}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
