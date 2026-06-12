/**
 * Pure visual-decision helpers for the graph primitives. Kept
 * framework-free and side-effect-free so they can be unit
 * tested directly; the React components are thin renderers over these.
 */

import { cardinalityLabel } from "@/lib/entities";
import type { Cardinality, PiiLevel } from "@/lib/types/meta";

export type GraphNodeGroup = "identity" | "billing" | "activity" | "other";

/**
 * Halo colour for the PII-heat overlay. The catastrophic alarm is always on
 * (the node's reserved ring); the overlay adds a softer halo to entities that
 * actually carry PII. Only the real-PII tiers light: `catastrophic` → alarm,
 * `pii` → the secondary PII token. `confidential` / `internal` are
 * sensitivity bands, NOT PII categories, so they get no halo (mirrors the
 * "map only pii" rule on the PII matrix); `none` is null. Returns null when
 * no halo should render.
 */
export function graphPiiHaloColor(piiLevel: PiiLevel): string | null {
  if (piiLevel === "catastrophic") return "var(--alarm)";
  if (piiLevel === "pii") return "var(--pii-2)";
  return null;
}

/** Group → accent colour token. Mirrors the handoff legend. */
export const GROUP_COLOR: Record<GraphNodeGroup, string> = {
  identity: "var(--green)",
  billing: "var(--cyan)",
  activity: "var(--violet)",
  other: "var(--ink-3)",
};

/**
 * Node accent colour. Catastrophic PII always wins (reserved alarm), otherwise
 * the entity's group colour.
 */
export function graphNodeColor(group: GraphNodeGroup, catastrophic = false): string {
  return catastrophic ? "var(--alarm)" : GROUP_COLOR[group];
}

/**
 * Border colour of a node's orb. Matches the handoff: catastrophic → the
 * reserved alarm; selected → green; otherwise a NEUTRAL glass stroke. The group
 * colour does NOT live on the ring — it is carried solely by the bright core
 * dot inside the orb (so the disc reads as neutral glass, not a coloured ring).
 * A separate outer green ring is also drawn on selection (see GraphNode) so the
 * pick stays visible even on a catastrophic node.
 */
export function graphNodeRing(catastrophic = false, selected = false): string {
  if (catastrophic) return "var(--alarm)";
  if (selected) return "var(--green)";
  return "var(--glass-stroke)";
}

export interface GraphEdgeStyleInput {
  /** Declared FK (solid) vs log-mined (dashed). */
  declared: boolean;
  /** On the highlighted canonical path. */
  highlighted?: boolean;
  /** Log-mined emphasis overlay is active. */
  minedEmphasis?: boolean;
  /**
   * Incident to the currently selected node — brightened so the picked
   * entity's relationships read at a glance. Ranks below the path / mined
   * emphasis (those keep their colour) but above the resting + dimmed states.
   */
  selectedIncident?: boolean;
  /**
   * A focus overlay (search or PII-heat) is active and this edge is not the
   * subject — fade it back so the highlighted/mined edges read clearly.
   * Ignored for highlighted / mined-emphasis / selected-incident edges (they
   * stay prominent).
   */
  dimmed?: boolean;
}

export interface GraphEdgeStyle {
  stroke: string;
  strokeWidth: number;
  strokeDasharray: string | undefined;
  opacity: number;
  /** SVG filter (the canonical-path glow); undefined for every other edge. */
  filter?: string;
}

/** Soft green halo on the canonical-path edge (the handoff's `#kgglow`). */
const PATH_GLOW = "drop-shadow(0 0 3px var(--green))";

/**
 * Edge stroke styling, in priority order: highlighted path (green, glowing) >
 * log-mined emphasis (cyan) > selected-incident (brightened neutral) >
 * resting (hairline). The solid/dashed distinction is independent and always
 * reflects declared-vs-mined.
 */
export function graphEdgeStyle({
  declared,
  highlighted = false,
  minedEmphasis = false,
  selectedIncident = false,
  dimmed = false,
}: GraphEdgeStyleInput): GraphEdgeStyle {
  const dash = declared ? undefined : "4 5";
  if (highlighted) {
    return {
      stroke: "var(--green)",
      strokeWidth: 2.6,
      strokeDasharray: dash,
      opacity: 0.95,
      filter: PATH_GLOW,
    };
  }
  if (minedEmphasis) {
    return { stroke: "var(--cyan)", strokeWidth: 2, strokeDasharray: dash, opacity: 0.9 };
  }
  if (selectedIncident) {
    // A brighter NEUTRAL ink — clearly forward, but never green/cyan (which
    // would imply a relationship type this edge doesn't have).
    return { stroke: "var(--ink-2)", strokeWidth: 2, strokeDasharray: dash, opacity: 0.95 };
  }
  // Resting edge — faded under an active focus overlay, else a quiet but
  // READABLE line. Uses --ink-3 (the tertiary ink, contrast-verified ≥4.5:1
  // on the canvas --bg-1 in both themes), NOT --hair: --hair is a near-white
  // panel-border hairline that vanished against the graph canvas, so resting
  // edges (e.g. the rank-0 demo joins) rendered invisible. opacity 0.7 keeps
  // them clearly subordinate to highlighted (green, 0.95) and selected
  // (--ink-2, 0.95) edges while still being visible.
  return {
    stroke: "var(--ink-3)",
    strokeWidth: 1.4,
    strokeDasharray: dash,
    opacity: dimmed ? 0.16 : 0.7,
  };
}

export interface GraphEdgeLabel {
  text: string;
  color: string;
}

export interface GraphEdgeLabelInput {
  highlighted?: boolean;
  selectedIncident?: boolean;
  minedEmphasis?: boolean;
  /** Declared-FK-only cardinality; null for mined / pre-existing edges. */
  cardinality: Cardinality | null;
}

/**
 * The midpoint label an edge should carry, or null for none. Only emphasised
 * edges are labelled (path, selected-incident, or the log-mined overlay) —
 * resting edges stay clean. A mined-emphasis edge reads "mined" (it has no
 * declared cardinality by contract); an emphasised declared edge reads its
 * compact cardinality ("N:1"). An emphasised edge whose cardinality is unknown
 * gets NO label rather than a fabricated one. Colour mirrors the stroke
 * emphasis: green on the path, cyan when mined, muted ink when selected.
 */
export function graphEdgeLabel({
  highlighted = false,
  selectedIncident = false,
  minedEmphasis = false,
  cardinality,
}: GraphEdgeLabelInput): GraphEdgeLabel | null {
  if (minedEmphasis) return { text: "mined", color: "var(--cyan)" };
  if (!highlighted && !selectedIncident) return null;
  const text = cardinalityLabel(cardinality);
  if (text === null) return null;
  return { text, color: highlighted ? "var(--green)" : "var(--ink-2)" };
}
