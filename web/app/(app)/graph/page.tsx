import { Graph } from "@/components/graph/Graph";

/**
 * Knowledge-graph surface (/graph), mounted inside the app shell (the shell
 * owns the chrome — top bar, nav rail, source selector, theme, and the global
 * `?entity=` drilldown sheet).
 *
 * The signature surface: a read-only map of the indexed schema — entities as
 * nodes, canonical joins as edges, the canonical path traced through, and
 * catastrophic-PII entities flagged. Selection is URL-driven (`?entity=`).
 */
export default function GraphPage() {
  return <Graph />;
}
