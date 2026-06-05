import { Handle, type NodeProps, Position } from "reactflow";
import type { PiiLevel } from "@/lib/types/meta";
import {
  GROUP_COLOR,
  graphNodeRing,
  graphPiiHaloColor,
  type GraphNodeGroup,
} from "./graphStyle";

// Hidden connection anchors. The graph is a read-only projection — nodes are
// never wired up by hand — so the handles are non-connectable and invisible;
// they exist only to give reactflow a deterministic point to anchor each edge.
// A target on the left and a source on the right keep edge beziers reading
// left-to-right along the canonical-path backbone the layout lays down.
const HANDLE_STYLE: React.CSSProperties = {
  opacity: 0,
  width: 1,
  height: 1,
  minWidth: 1,
  minHeight: 1,
  border: 0,
  background: "transparent",
  pointerEvents: "none",
};

export interface GraphNodeData {
  label: string;
  group: GraphNodeGroup;
  /** Catastrophic PII present on the entity — reserved alarm treatment. */
  catastrophic?: boolean;
  rowCount?: number | null;
  /**
   * Full live PII severity (drives the non-catastrophic halo). The
   * catastrophic alarm ring keys off `catastrophic`; this drives the softer
   * `pii`-tier halo when the PII-heat overlay is on. One source of truth — do
   * NOT re-derive catastrophic-ness from this client-side.
   */
  piiLevel?: PiiLevel;
  /** PII-heat overlay active → render the halo for PII-bearing entities. */
  piiHeat?: boolean;
  /** Live attributed refusal count (0 when none). */
  refusalCount?: number;
  /** Refusal-hotspot overlay active → render the count badge + pulse ring. */
  refusalActive?: boolean;
}

/**
 * reactflow-ready entity node primitive (look only). Group colour via fill +
 * ring; catastrophic PII via the reserved alarm ring + glow + label; selection
 * via the green ring. Two PR-17b overlay treatments ride on the same node so
 * the graph has a single node renderer: a softer PII halo (PII-heat overlay,
 * `pii`-tier entities) and a refusal-hotspot badge + pulse ring (refusal
 * overlay, entities with attributed refusals). Both are gated by overlay-active
 * flags set at render time, so the resting node is unchanged. The pulse is a
 * CSS animation disabled under `prefers-reduced-motion`.
 *
 * Typed against reactflow's NodeProps so the graph surface can drop it into
 * `nodeTypes`; it pulls no reactflow runtime beyond the type-only import + the
 * Handle anchors, so it renders in isolation for tests.
 */
export function GraphNode({ data, selected }: NodeProps<GraphNodeData>) {
  const { label, group, catastrophic = false, piiLevel = "none" } = data;
  const accent = GROUP_COLOR[group];
  const ring = graphNodeRing(group, catastrophic, selected);
  const emphasised = catastrophic || selected;
  const diameter = catastrophic ? 30 : 22;

  const haloColor = data.piiHeat ? graphPiiHaloColor(piiLevel) : null;
  const refusalCount = data.refusalCount ?? 0;
  const showRefusal = (data.refusalActive ?? false) && refusalCount > 0;

  return (
    <div
      className="sb-gnode"
      data-group={group}
      data-catastrophic={catastrophic ? "true" : undefined}
      style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "var(--s1)" }}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} style={HANDLE_STYLE} />
      <Handle type="source" position={Position.Right} isConnectable={false} style={HANDLE_STYLE} />
      <div style={{ position: "relative", width: diameter, height: diameter }}>
        {haloColor && (
          // Soft PII halo behind the orb. `pointerEvents: none` so it never
          // intercepts the node drag/click.
          <span
            aria-hidden
            data-pii-halo="true"
            style={{
              position: "absolute",
              inset: "-12px",
              borderRadius: "50%",
              background: haloColor,
              opacity: 0.16,
              border: `1.2px solid ${haloColor}`,
              pointerEvents: "none",
            }}
          />
        )}
        {showRefusal && (
          // Refusal-hotspot pulse ring behind the orb. The animation lives in
          // kit.css (`.sb-gnode-pulse`) — compositor-only (transform+opacity)
          // and disabled under `prefers-reduced-motion`, so it never intercepts
          // input and respects the parked-motion ethos.
          <span aria-hidden data-refusal-pulse="true" className="sb-gnode-pulse" />
        )}
        <div
          style={{
            position: "relative",
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
        {showRefusal && (
          <span
            className="sb-gnode-refusal"
            data-refusal-badge="true"
            aria-label={`${refusalCount} refusal${refusalCount === 1 ? "" : "s"}`}
            style={{
              position: "absolute",
              top: -6,
              right: -6,
              minWidth: 16,
              height: 16,
              padding: "0 4px",
              borderRadius: 8,
              background: "var(--alarm)",
              color: "var(--alarm-ink)",
              fontFamily: "var(--f-mono)",
              fontSize: 10,
              fontWeight: 700,
              lineHeight: "16px",
              textAlign: "center",
              boxShadow: "0 0 0 2px var(--bg-1)",
              pointerEvents: "none",
            }}
          >
            {refusalCount}
          </span>
        )}
      </div>
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
