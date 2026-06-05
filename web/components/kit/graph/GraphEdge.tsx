import { BaseEdge, getStraightPath, type EdgeProps } from "reactflow";
import type { Cardinality } from "@/lib/types/meta";
import { graphEdgeLabel, graphEdgeStyle } from "./graphStyle";

export interface GraphEdgeData {
  /** Declared FK (solid) vs log-mined (dashed). */
  declared: boolean;
  /** On the highlighted canonical path. */
  highlighted?: boolean;
  /** Log-mined emphasis overlay active. */
  minedEmphasis?: boolean;
  /** Incident to the currently selected node — brightened (G3). */
  selectedIncident?: boolean;
  /** Faded back because a focus overlay (search / PII heat) is active. */
  dimmed?: boolean;
  /** Declared-FK-only cardinality; null for mined / pre-existing edges (G1). */
  cardinality?: Cardinality | null;
}

/**
 * reactflow-ready relationship edge primitive (look only). Straight spokes
 * (matching the handoff) — solid for declared FKs, dashed for log-mined joins;
 * green+glow on the canonical path, cyan under the log-mined overlay, brightened
 * neutral when incident to the selected node, hairline at rest. Emphasised edges
 * carry a midpoint label — the compact cardinality ("N:1") for declared edges,
 * "mined" under the log-mined overlay — drawn as SVG text in reactflow's own
 * edge layer (no portal needed). Typed against reactflow's EdgeProps.
 */
export function GraphEdge({
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  data,
}: EdgeProps<GraphEdgeData>) {
  const [edgePath, labelX, labelY] = getStraightPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
  });
  const { stroke, strokeWidth, strokeDasharray, opacity, filter } = graphEdgeStyle({
    declared: data?.declared ?? true,
    highlighted: data?.highlighted,
    minedEmphasis: data?.minedEmphasis,
    selectedIncident: data?.selectedIncident,
    dimmed: data?.dimmed,
  });
  const label = graphEdgeLabel({
    highlighted: data?.highlighted,
    selectedIncident: data?.selectedIncident,
    minedEmphasis: data?.minedEmphasis,
    cardinality: data?.cardinality ?? null,
  });

  return (
    <>
      <BaseEdge
        path={edgePath}
        markerEnd={markerEnd}
        style={{ stroke, strokeWidth, strokeDasharray, opacity, filter }}
      />
      {label && (
        <text
          x={labelX}
          y={labelY - 6}
          textAnchor="middle"
          style={{
            fontFamily: "var(--f-mono)",
            fontSize: 10.5,
            fontWeight: 600,
            fill: label.color,
            pointerEvents: "none",
            userSelect: "none",
          }}
        >
          {label.text}
        </text>
      )}
    </>
  );
}
