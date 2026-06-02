import type { NodeProps } from "reactflow";
import {
  GROUP_COLOR,
  graphNodeRing,
  type GraphNodeGroup,
} from "./graphStyle";

export interface GraphNodeData {
  label: string;
  group: GraphNodeGroup;
  /** Catastrophic PII present on the entity — reserved alarm treatment. */
  catastrophic?: boolean;
  rowCount?: number | null;
}

/**
 * reactflow-ready entity node primitive (look only). Group colour via fill +
 * ring, catastrophic PII via the reserved alarm ring + glow + label, selection
 * via the green ring. Typed against reactflow's NodeProps so the graph surface
 * can drop it into `nodeTypes`; it pulls no reactflow runtime (type-only import),
 * so it renders in isolation for tests.
 */
export function GraphNode({ data, selected }: NodeProps<GraphNodeData>) {
  const { label, group, catastrophic = false } = data;
  const accent = GROUP_COLOR[group];
  const ring = graphNodeRing(group, catastrophic, selected);
  const emphasised = catastrophic || selected;
  const diameter = catastrophic ? 30 : 22;

  return (
    <div
      className="sb-gnode"
      data-group={group}
      data-catastrophic={catastrophic ? "true" : undefined}
      style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "var(--s1)" }}
    >
      <div
        style={{
          width: diameter,
          height: diameter,
          borderRadius: "50%",
          background: `color-mix(in oklch, ${accent} 16%, var(--glass))`,
          WebkitBackdropFilter: "blur(4px)",
          backdropFilter: "blur(4px)",
          border: `${catastrophic ? 2 : 1.4}px solid ${ring}`,
          boxShadow: emphasised ? "var(--glow-green)" : "none",
        }}
      />
      <span
        style={{
          fontFamily: "var(--f-mono)",
          fontSize: 11,
          fontWeight: 600,
          color: "var(--ink)",
          whiteSpace: "nowrap",
        }}
      >
        {label}
      </span>
      {catastrophic && (
        <span
          style={{
            fontFamily: "var(--f-mono)",
            fontSize: 8,
            fontWeight: 700,
            letterSpacing: "0.08em",
            color: "var(--alarm)",
          }}
        >
          CATASTROPHIC
        </span>
      )}
    </div>
  );
}
