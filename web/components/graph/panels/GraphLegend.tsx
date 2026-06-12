"use client";

import { useState } from "react";
import { Icon } from "@/components/kit";
import styles from "../graph.module.css";

/** The CLI command the read-only "Re-index" affordance copies. The dashboard
 *  never mutates the store (ADR 0006) — actions hand back the command. */
const REINDEX_COMMAND = "schemabrain index";

interface GraphLegendProps {
  /** Whether any edge in the projection is log-mined / inferred (non-declared).
   *  When the schema is pure-FK we drop the provenance footnote — there is
   *  nothing dashed on screen to explain. */
  hasMinedEdges: boolean;
}

/**
 * Top-right legend + the read-only re-index action. The provenance footnote is
 * the honesty line: a dashed edge is recovered from query logs, not a declared
 * FK, and cardinality is shown only for declared FK edges (an unverified shape
 * is never rendered as engine-derived).
 */
export function GraphLegend({ hasMinedEdges }: GraphLegendProps) {
  const [copied, setCopied] = useState(false);

  const copyReindex = () => {
    void navigator.clipboard?.writeText(REINDEX_COMMAND);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <div className={`${styles.panel} ${styles.legend}`}>
      <h4>Legend</h4>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          <svg width="14" height="14" aria-hidden>
            <circle cx="7" cy="7" r="6" fill="none" stroke="var(--alarm)" strokeWidth="2" />
          </svg>
        </span>
        Catastrophic PII
      </div>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          {/* Solid badge (vs the hollow ring above): the firewall has actually
              refused agent calls here. Behavioural evidence, not static risk. */}
          <svg width="14" height="14" aria-hidden>
            <circle cx="7" cy="7" r="5.5" fill="var(--alarm)" />
          </svg>
        </span>
        Refusal hotspot
      </div>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          <svg width="22" height="6" aria-hidden>
            <line x1="0" y1="3" x2="22" y2="3" stroke="var(--ink-2)" strokeWidth="2" />
          </svg>
        </span>
        Declared FK
      </div>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          <svg width="22" height="6" aria-hidden>
            <line
              x1="0"
              y1="3"
              x2="22"
              y2="3"
              stroke="var(--cyan)"
              strokeWidth="2"
              strokeDasharray="4 4"
            />
          </svg>
        </span>
        Log-mined join
      </div>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          <svg width="14" height="14" aria-hidden>
            <circle cx="7" cy="7" r="3.5" fill="var(--green)" />
          </svg>
        </span>
        Identity
      </div>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          <svg width="14" height="14" aria-hidden>
            <circle cx="7" cy="7" r="3.5" fill="var(--cyan)" />
          </svg>
        </span>
        Billing
      </div>
      <div className={styles.legendRow}>
        <span className={styles.legendKey}>
          <svg width="14" height="14" aria-hidden>
            <circle cx="7" cy="7" r="3.5" fill="var(--violet)" />
          </svg>
        </span>
        Activity
      </div>
      {hasMinedEdges && (
        <p className={styles.legendNote}>
          Dashed edges are recovered from query logs, not declared FKs. Cardinality shows on
          declared FK edges only — a mined shape is never rendered as engine-verified.
        </p>
      )}
      <div className={styles.actions}>
        <button type="button" className={styles.reindex} onClick={copyReindex}>
          <Icon name={copied ? "check" : "rotate-cw"} size={13} />
          {copied ? "Copied" : "Copy re-index command"}
        </button>
      </div>
    </div>
  );
}
