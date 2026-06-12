import type { CSSProperties } from "react";
import { Icon } from "@/components/kit";
import styles from "../graph.module.css";

/** The three graph overlays, all backed by real projection / audit data. */
export interface OverlayState {
  /** PII-heat halo on PII-bearing entities. */
  pii: boolean;
  /** Refusal-hotspot badges from attributed `mcp_audit` refusals. */
  refusals: boolean;
  /** Cyan emphasis on log-mined (non-declared) edges. */
  mined: boolean;
}

export type OverlayKey = keyof OverlayState;

interface ToggleSpec {
  key: OverlayKey;
  label: string;
  accent: string;
}

const TOGGLES: readonly ToggleSpec[] = [
  { key: "pii", label: "PII heat", accent: "var(--alarm)" },
  { key: "refusals", label: "Refusal hotspots", accent: "var(--alarm)" },
  { key: "mined", label: "Log-mined joins", accent: "var(--cyan)" },
];

interface GraphToolsProps {
  search: string;
  onSearchChange: (value: string) => void;
  overlays: OverlayState;
  onToggle: (key: OverlayKey) => void;
  /** Refused rows not shown on any visible node (no anchor, or an anchor for
   *  an entity no longer in the graph). Shown under the refusal toggle so the
   *  badges + this figure reconcile with the audit log. */
  unattributedRefusals: number;
}

/**
 * Top-left tools panel: entity search + the three data-backed overlays. The
 * refusal toggle, when on, surfaces the honest "unattributed" remainder so the
 * per-node badge totals always reconcile with the audit log (PR-17b).
 */
export function GraphTools({
  search,
  onSearchChange,
  overlays,
  onToggle,
  unattributedRefusals,
}: GraphToolsProps) {
  return (
    <div className={`${styles.panel} ${styles.tools}`}>
      <div className={styles.search}>
        <Icon name="search" size={15} label="search entities" />
        <input
          type="search"
          placeholder="Search entities…"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          aria-label="Search entities"
        />
      </div>
      <div className={styles.overlays} role="group" aria-label="Graph overlays">
        {TOGGLES.map(({ key, label, accent }) => {
          const on = overlays[key];
          return (
            <button
              key={key}
              type="button"
              className={on ? `${styles.toggle} ${styles.toggleOn}` : styles.toggle}
              style={{ "--accent": accent } as CSSProperties}
              aria-pressed={on}
              onClick={() => onToggle(key)}
            >
              <span className={styles.swatch} />
              {label}
            </button>
          );
        })}
      </div>
      {overlays.refusals && (
        <>
          <p className={styles.hotspotDef}>
            Where the firewall blocked an agent&rsquo;s call — the badge counts refusals logged for
            that entity (not the same as the catastrophic-PII ring).
          </p>
          {unattributedRefusals > 0 && (
            <p className={styles.unattributed}>
              + {unattributedRefusals} refusal{unattributedRefusals === 1 ? "" : "s"} not attributed
              to a visible entity
            </p>
          )}
        </>
      )}
    </div>
  );
}
