/**
 * Pure visual-decision helpers for the graph primitives. Kept
 * framework-free and side-effect-free so they can be unit
 * tested directly; the React components are thin renderers over these.
 */

export type GraphNodeGroup = "identity" | "billing" | "activity" | "other";

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
}

export interface GraphEdgeStyle {
  stroke: string;
  strokeWidth: number;
  strokeDasharray: string | undefined;
  opacity: number;
}

/**
 * Edge stroke styling, in priority order: highlighted path (green, glowing) >
 * log-mined emphasis (cyan) > resting (hairline). The solid/dashed distinction
 * is independent and always reflects declared-vs-mined.
 */
export function graphEdgeStyle({
  declared,
  highlighted = false,
  minedEmphasis = false,
}: GraphEdgeStyleInput): GraphEdgeStyle {
  const dash = declared ? undefined : "4 5";
  if (highlighted) {
    return { stroke: "var(--green)", strokeWidth: 2.6, strokeDasharray: dash, opacity: 0.95 };
  }
  if (minedEmphasis) {
    return { stroke: "var(--cyan)", strokeWidth: 2, strokeDasharray: dash, opacity: 0.9 };
  }
  return { stroke: "var(--hair)", strokeWidth: 1.4, strokeDasharray: dash, opacity: 0.5 };
}
