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
 * Ring colour around a node. Catastrophic → alarm; selected → green; else the
 * group accent so the cluster colour stays legible.
 */
export function graphNodeRing(
  group: GraphNodeGroup,
  catastrophic = false,
  selected = false,
): string {
  if (catastrophic) return "var(--alarm)";
  if (selected) return "var(--green)";
  return GROUP_COLOR[group];
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
}

/**
 * Edge stroke styling, in priority order: highlighted path (green, glowing) >
 * log-mined emphasis (cyan) > selected-incident (brightened hairline) >
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
    return { stroke: "var(--green)", strokeWidth: 2.6, strokeDasharray: dash, opacity: 0.95 };
  }
  if (minedEmphasis) {
    return { stroke: "var(--cyan)", strokeWidth: 2, strokeDasharray: dash, opacity: 0.9 };
  }
  if (selectedIncident) {
    // Same hairline colour, brought forward — selection brightens, it never
    // recolours (recolouring would imply a relationship type it doesn't have).
    return { stroke: "var(--hair)", strokeWidth: 1.8, strokeDasharray: dash, opacity: 0.9 };
  }
  // Resting edge — faded under an active focus overlay, else the hairline rest.
  return {
    stroke: "var(--hair)",
    strokeWidth: 1.4,
    strokeDasharray: dash,
    opacity: dimmed ? 0.16 : 0.5,
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
