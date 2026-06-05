"use client";

// The ONLY module that pulls the reactflow runtime + its stylesheet. It is
// reached exclusively through `next/dynamic(() => import("./GraphCanvas"),
// { ssr: false })` in Graph.tsx, so reactflow lives in its own client chunk
// and never enters the landing or server bundle (the publish wheel sentinel
// asserts reactflow is absent from the landing entry).

import "reactflow/dist/style.css";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  type NodeDragHandler,
  type NodeMouseHandler,
  useEdgesState,
  useNodesState,
} from "reactflow";

import { GraphEdge } from "@/components/kit/graph/GraphEdge";
import { GraphNode } from "@/components/kit/graph/GraphNode";
import { GROUP_COLOR, graphPiiHaloColor } from "@/components/kit/graph/graphStyle";
import { useReducedMotion } from "@/lib/useReducedMotion";
import type { GraphResponse } from "@/lib/types/graph";

import {
  ENTITY_NODE_TYPE,
  RELATIONSHIP_EDGE_TYPE,
  adaptGraph,
  type GraphFlowEdge,
  type GraphFlowNode,
  type GraphFlowNodeData,
} from "./graphAdapter";
import {
  computeSeedPositions,
  createSimNodes,
  isParked,
  type SimNode,
  settle,
  stepSim,
} from "./graphLayout";
import { GraphLegend } from "./panels/GraphLegend";
import { GraphPath } from "./panels/GraphPath";
import { GraphTooltip } from "./panels/GraphTooltip";
import { GraphTools, type OverlayKey, type OverlayState } from "./panels/GraphTools";
import styles from "./graph.module.css";

// Stable at module scope — reactflow warns (and re-instantiates node renderers)
// if these objects change identity between renders.
const NODE_TYPES = { [ENTITY_NODE_TYPE]: GraphNode };
const EDGE_TYPES = { [RELATIONSHIP_EDGE_TYPE]: GraphEdge };

// Positions are node *centres* (matches the layout's coordinate semantics and
// the handoff's centre-based seeds), not top-left corners.
const NODE_ORIGIN: [number, number] = [0.5, 0.5];
const FIT_VIEW_OPTIONS = { padding: 0.2 };
const NO_OVERLAYS: OverlayState = { pii: false, refusals: false, mined: false };

export interface GraphCanvasProps {
  graph: GraphResponse;
  /** Entity id of the open drilldown (`?entity=`), or null. Drives the ring. */
  selectedId: string | null;
  /** Open the entity drilldown — Graph.tsx pushes `?entity=<id>`. */
  onOpenEntity: (id: string) => void;
  /** Drop `?entity=` (pane click). */
  onClearSelection: () => void;
}

interface Built {
  nodes: GraphFlowNode[];
  edges: GraphFlowEdge[];
  sim: SimNode[];
  by: Map<string, SimNode>;
}

interface TooltipState {
  id: string;
  x: number;
  y: number;
}

/** Adapt the projection, generate deterministic seeds, and pre-settle the sim
 *  silently so the first painted frame is already at rest. */
function build(graph: GraphResponse): Built {
  const adapted = adaptGraph(graph);
  const seeds = computeSeedPositions({
    nodeIds: adapted.nodes.map((n) => n.id),
    edges: adapted.edges.map((e) => [e.source, e.target] as const),
    spine: graph.canonical_path.nodes,
  });
  const sim = createSimNodes(seeds);
  settle(sim);
  const by = new Map(sim.map((s) => [s.id, s]));
  const nodes = adapted.nodes.map((n) => {
    const s = by.get(n.id);
    return s ? { ...n, position: { x: s.x, y: s.y } } : n;
  });
  return { nodes, edges: adapted.edges, sim, by };
}

/**
 * The signature knowledge-graph canvas reproduced on reactflow (the locked
 * "reproduce-on-reactflow" decision): kit GraphNode/GraphEdge primitives, free
 * pan/zoom (MiniMap + Controls), deterministic generated layout, and the
 * handoff's restore-to-seed force sim driven by a *parked* rAF loop — it runs
 * only while a drag is in flight or the layout still has energy, then stops
 * (0% idle CPU).
 *
 * PR-17b layers the floating chrome on top: search + the three data-backed
 * overlays (PII heat, refusal hotspots, log-mined joins), the legend with the
 * read-only re-index affordance, the canonical-path chip, and a hover tooltip.
 * Overlay/search effects are derived immutably from base state at render time
 * (`displayNodes`/`displayEdges`) so they never fight the sim's position
 * writes.
 *
 * Motion is gated: under `prefers-reduced-motion` the loop is never started.
 * Selection is the URL (`?entity=`): the open drilldown and the node's green
 * ring share one source of truth.
 *
 * `data-graph-loop` / `data-graph-frames` / `data-reduced-motion` are exposed
 * on the frame for the Playwright spec to assert the loop runs *and* stops.
 */
export default function GraphCanvas({
  graph,
  selectedId,
  onOpenEntity,
  onClearSelection,
}: GraphCanvasProps) {
  const reducedMotion = useReducedMotion();
  const canvasRef = useRef<HTMLDivElement | null>(null);

  // Built once. Graph.tsx remounts this component (key=source) on a source
  // switch, so the mutable sim ref can never outlive its graph.
  const builtRef = useRef<Built | null>(null);
  if (builtRef.current === null) builtRef.current = build(graph);
  const built = builtRef.current;

  const [nodes, setNodes, onNodesChange] = useNodesState(built.nodes);
  const [edges, , onEdgesChange] = useEdgesState(built.edges);

  // Overlay + search interaction state (NOT projection data — layered on at
  // render time below). Reset for free on a source switch (Graph.tsx remounts).
  const [overlays, setOverlays] = useState<OverlayState>(NO_OVERLAYS);
  const [search, setSearch] = useState("");
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);

  // rAF bookkeeping. `frames` is the parked frame count (0 until the loop has
  // run); `loopState` lets the spec see running↔parked transitions.
  const loop = useRef({ running: false, raf: 0, dragging: false, frames: 0 });
  const [frames, setFrames] = useState(0);
  const [loopState, setLoopState] = useState<"running" | "parked">("parked");

  const writePositions = useCallback(() => {
    setNodes((current) =>
      current.map((n) => {
        const s = built.by.get(n.id);
        return s ? { ...n, position: { x: s.x, y: s.y } } : n;
      }),
    );
  }, [built, setNodes]);

  const kick = useCallback(() => {
    if (reducedMotion || loop.current.running) return; // motion guard / already live
    loop.current.running = true;
    setLoopState("running");
    const step = () => {
      const energy = stepSim(built.sim);
      loop.current.frames += 1;
      writePositions();
      if (isParked(energy) && !loop.current.dragging) {
        loop.current.running = false;
        setFrames(loop.current.frames);
        setLoopState("parked");
        return;
      }
      loop.current.raf = requestAnimationFrame(step);
    };
    loop.current.raf = requestAnimationFrame(step);
  }, [reducedMotion, built, writePositions]);

  // Cancel any in-flight frame on unmount.
  useEffect(() => () => cancelAnimationFrame(loop.current.raf), []);

  // URL → ring. The node's `selected` flag mirrors `?entity=` so the open
  // drilldown and the highlighted node never disagree.
  useEffect(() => {
    setNodes((current) =>
      current.map((n) => {
        const next = n.id === selectedId;
        return n.selected === next ? n : { ...n, selected: next };
      }),
    );
  }, [selectedId, setNodes]);

  // ── Derived view: overlay + search effects, computed immutably so they never
  //    fight the sim's position writes (which mutate the base `nodes` state).
  const pathNodes = useMemo(() => new Set(graph.canonical_path.nodes), [graph]);

  const matched = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return null;
    return new Set(nodes.filter((n) => n.data.label.toLowerCase().includes(q)).map((n) => n.id));
  }, [search, nodes]);

  const displayNodes = useMemo<GraphFlowNode[]>(() => {
    return nodes.map((n) => {
      // Honest dimming: a node fades when a focus is active and it is NOT the
      // subject of that focus — a search miss, OR PII-heat on + no PII, OR the
      // refusal overlay on + no refusals (and off the canonical spine).
      const searchMiss = matched !== null && !matched.has(n.id);
      const piiMiss = overlays.pii && graphPiiHaloColor(n.data.piiLevel) === null;
      const refusalMiss =
        overlays.refusals && (n.data.refusalCount ?? 0) === 0 && !pathNodes.has(n.id);
      const isDimmed = searchMiss || piiMiss || refusalMiss;
      return {
        ...n,
        data: { ...n.data, piiHeat: overlays.pii, refusalActive: overlays.refusals },
        style: { ...n.style, opacity: isDimmed ? 0.32 : 1, transition: "opacity 200ms" },
      };
    });
  }, [nodes, matched, overlays, pathNodes]);

  const displayEdges = useMemo<GraphFlowEdge[]>(() => {
    const focusActive = matched !== null || overlays.pii;
    return edges.map((e) => {
      // G3: an edge touching the selected node is brightened + labelled.
      const selectedIncident = selectedId !== null && (e.source === selectedId || e.target === selectedId);
      const highlighted = e.data?.highlighted ?? false;
      return {
        ...e,
        // G2: directional arrowhead — green + larger on the canonical path,
        // muted ink elsewhere. `userSpaceOnUse` keeps a constant pixel size
        // instead of scaling with the edge's stroke width. The arrow colour is
        // applied via CSS `fill`/`stroke` (reactflow's ArrowClosedSymbol), so
        // the theme tokens resolve through the cascade.
        markerEnd: {
          type: MarkerType.ArrowClosed,
          markerUnits: "userSpaceOnUse",
          width: highlighted ? 16 : 13,
          height: highlighted ? 16 : 13,
          color: highlighted ? "var(--green)" : "var(--ink-3)",
        },
        data: e.data
          ? {
              ...e.data,
              minedEmphasis: overlays.mined && !e.data.declared,
              selectedIncident,
              // A selected-incident edge is itself the focus → never dim it.
              dimmed: focusActive && !e.data.highlighted && !selectedIncident,
            }
          : e.data,
      };
    });
  }, [edges, matched, overlays, selectedId]);

  const pinTo = useCallback(
    (id: string, x: number, y: number) => {
      const s = built.by.get(id);
      if (s) {
        s.fx = x;
        s.fy = y;
      }
    },
    [built],
  );

  const onNodeDragStart: NodeDragHandler = useCallback(
    (_, node) => {
      loop.current.dragging = true;
      pinTo(node.id, node.position.x, node.position.y);
      kick();
    },
    [pinTo, kick],
  );

  const onNodeDrag: NodeDragHandler = useCallback(
    (_, node) => {
      pinTo(node.id, node.position.x, node.position.y);
      kick();
    },
    [pinTo, kick],
  );

  const onNodeDragStop: NodeDragHandler = useCallback(
    (_, node) => {
      const s = built.by.get(node.id);
      if (s) {
        s.fx = null;
        s.fy = null;
      }
      loop.current.dragging = false;
      kick(); // spring the released node back toward its seed, then park
    },
    [built, kick],
  );

  const onNodeClick: NodeMouseHandler = useCallback(
    (_, node) => onOpenEntity(node.id),
    [onOpenEntity],
  );

  const onNodeMouseEnter: NodeMouseHandler = useCallback((event, node) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    setTooltip({ id: node.id, x: event.clientX - rect.left, y: event.clientY - rect.top });
  }, []);

  const onNodeMouseLeave: NodeMouseHandler = useCallback(() => setTooltip(null), []);

  const toggleOverlay = useCallback((key: OverlayKey) => {
    setOverlays((current) => ({ ...current, [key]: !current[key] }));
  }, []);

  const tooltipNode = tooltip ? graph.nodes.find((n) => n.id === tooltip.id) : undefined;
  const hasMinedEdges = useMemo(
    () => graph.edges.some((e) => e.evidence !== "declared"),
    [graph],
  );

  return (
    <div
      ref={canvasRef}
      className={styles.canvas}
      role="region"
      aria-label="Knowledge graph"
      data-graph-loop={loopState}
      data-graph-frames={frames}
      data-reduced-motion={reducedMotion ? "true" : "false"}
    >
      <ReactFlow
        nodes={displayNodes}
        edges={displayEdges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onNodeMouseEnter={onNodeMouseEnter}
        onNodeMouseLeave={onNodeMouseLeave}
        onPaneClick={onClearSelection}
        onNodeDragStart={onNodeDragStart}
        onNodeDrag={onNodeDrag}
        onNodeDragStop={onNodeDragStop}
        nodeOrigin={NODE_ORIGIN}
        nodesConnectable={false}
        elementsSelectable={false}
        nodesDraggable
        fitView
        fitViewOptions={FIT_VIEW_OPTIONS}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={34} color="var(--grid-line)" />
        <MiniMap
          pannable
          zoomable
          ariaLabel="Graph minimap"
          nodeColor={(node) => {
            const data = node.data as GraphFlowNodeData | undefined;
            if (!data) return "var(--ink-3)";
            return data.catastrophic ? "var(--alarm)" : (GROUP_COLOR[data.group] ?? "var(--ink-3)");
          }}
          maskColor="color-mix(in oklch, var(--bg-1) 70%, transparent)"
        />
        <Controls position="bottom-center" showInteractive={false} />
      </ReactFlow>

      <GraphTools
        search={search}
        onSearchChange={setSearch}
        overlays={overlays}
        onToggle={toggleOverlay}
        unattributedRefusals={graph.unattributed_refusals}
      />
      <GraphLegend hasMinedEdges={hasMinedEdges} />
      <GraphPath path={graph.canonical_path} />
      {tooltip && tooltipNode && (
        <GraphTooltip
          label={tooltipNode.label}
          rowCount={tooltipNode.row_count}
          piiLevel={tooltipNode.pii_level}
          x={tooltip.x}
          y={tooltip.y}
        />
      )}
    </div>
  );
}
