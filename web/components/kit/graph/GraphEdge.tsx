import { BaseEdge, getBezierPath, type EdgeProps } from "reactflow";
import { graphEdgeStyle } from "./graphStyle";

export interface GraphEdgeData {
  /** Declared FK (solid) vs log-mined (dashed). */
  declared: boolean;
  /** On the highlighted canonical path. */
  highlighted?: boolean;
  /** Log-mined emphasis overlay active. */
  minedEmphasis?: boolean;
}

/**
 * reactflow-ready relationship edge primitive (look only). Solid for declared
 * FKs, dashed for log-mined joins; green+glow on the canonical path, cyan under
 * the log-mined overlay, hairline at rest. Typed against reactflow's EdgeProps.
 */
export function GraphEdge({
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  markerEnd,
  data,
}: EdgeProps<GraphEdgeData>) {
  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const { stroke, strokeWidth, strokeDasharray, opacity } = graphEdgeStyle({
    declared: data?.declared ?? true,
    highlighted: data?.highlighted,
    minedEmphasis: data?.minedEmphasis,
  });

  return (
    <BaseEdge
      path={edgePath}
      markerEnd={markerEnd}
      style={{ stroke, strokeWidth, strokeDasharray, opacity }}
    />
  );
}
