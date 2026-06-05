import styles from "../graph.module.css";

interface GraphIndexingOverlayProps {
  /** The source being indexed (shown in the heading); falls back to a generic
   *  label when the id is unknown. */
  sourceLabel?: string | null;
}

/**
 * Indexing overlay — shown only while a source's REAL state is "indexing"
 * (a meta `SourceState` the sidecar reserves but does not emit today; this
 * surface lights up the moment it does, with no wiring change). It is honest
 * by construction: an indeterminate ring + the actual index phases, with NO
 * fabricated percentage or fake progress bar — the engine reports no live
 * progress, so we don't invent one. The ring is disabled under reduced motion.
 */
export function GraphIndexingOverlay({ sourceLabel }: GraphIndexingOverlayProps) {
  return (
    <div className={styles.indexing} role="status" aria-live="polite">
      <div className={styles.indexingRing} aria-hidden />
      <div className={styles.indexingTitle}>
        Indexing {sourceLabel ? sourceLabel : "this source"}…
      </div>
      <div className={styles.indexingBody}>
        describing tables · mining joins from query logs · embedding columns · binding business
        entities
      </div>
    </div>
  );
}
