import type { PiiLevel } from "@/lib/types/meta";
import styles from "../graph.module.css";

interface GraphTooltipProps {
  label: string;
  /** Cached row-count estimate; null renders "—", never a fabricated 0. */
  rowCount: number | null;
  piiLevel: PiiLevel;
  /** Position within the canvas container, in px (the node's screen centre). */
  x: number;
  y: number;
}

/** Human label for the node's PII severity, shown in the tooltip subline. */
function piiSummary(level: PiiLevel): string {
  switch (level) {
    case "catastrophic":
      return "catastrophic PII";
    case "pii":
      return "PII present";
    case "confidential":
      return "confidential";
    case "internal":
      return "internal";
    default:
      return "no PII";
  }
}

/**
 * Hover tooltip for a node — entity label + a row-count / PII subline. Pure
 * presentational; GraphCanvas owns the hovered node + its screen position and
 * unmounts this on leave. `pointer-events: none` (in CSS) so it never eats a
 * drag. A null row count renders "—" (never a fabricated 0).
 */
export function GraphTooltip({ label, rowCount, piiLevel, x, y }: GraphTooltipProps) {
  const rows = rowCount == null ? "—" : rowCount.toLocaleString();
  return (
    <div className={styles.tip} style={{ left: x, top: y }} role="tooltip">
      <div className={styles.tipTitle}>{label}</div>
      <div className={styles.tipSub}>
        {rows} rows · {piiSummary(piiLevel)}
      </div>
    </div>
  );
}
