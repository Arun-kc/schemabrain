import { Fragment } from "react";
import { Icon } from "@/components/kit";
import type { CanonicalPath } from "@/lib/types/graph";
import styles from "../graph.module.css";

interface GraphPathProps {
  path: CanonicalPath;
}

/**
 * Bottom-left canonical-path chip. Renders the diameter spine the backend
 * traced (`canonical_path.nodes`) with its hop count, both straight off the
 * wire — never a hardcoded path. A schema with no multi-hop chain
 * (`hops === 0` / a single entity) has no spine to draw, so we say so plainly
 * rather than render an empty arrow run.
 */
export function GraphPath({ path }: GraphPathProps) {
  const empty = path.hops === 0 || path.nodes.length === 0;

  return (
    <div className={`${styles.panel} ${styles.path}`}>
      <span style={{ color: "var(--green)", display: "inline-flex" }}>
        <Icon name="route" size={16} label="canonical path" />
      </span>
      <div>
        <div className={styles.pathLabel}>
          {empty ? "Canonical path" : `Canonical path · ${path.hops} hop${path.hops === 1 ? "" : "s"}`}
        </div>
        {empty ? (
          <div className={styles.pathLabel} style={{ textTransform: "none", letterSpacing: 0 }}>
            no multi-hop join chain yet
          </div>
        ) : (
          <div className={styles.hops}>
            {path.nodes.map((id, index) => (
              <Fragment key={id}>
                {index > 0 && <span className={styles.arrow}>→</span>}
                <span className={styles.hop}>{id}</span>
              </Fragment>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
