"use client";

// The ONLY module that pulls the reactflow runtime + its stylesheet. It is
// reached exclusively through `next/dynamic(() => import("./GraphCanvas"),
// { ssr: false })` in Graph.tsx, so reactflow lives in its own client chunk
// and never enters the landing or server bundle (the publish wheel sentinel
// asserts reactflow is absent from the landing entry).

import "reactflow/dist/style.css";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactFlow, {
  Background,
  type NodeDragHandler,
  type NodeMouseHandler,
  useEdgesState,
  useNodesState,
} from "reactflow";

import { GraphEdge } from "@/components/kit/graph/GraphEdge";
import { GraphNode } from "@/components/kit/graph/GraphNode";
import { useReducedMotion } from "@/lib/useReducedMotion";
import type { GraphResponse } from "@/lib/types/graph";

import {
  ENTITY_NODE_TYPE,
  RELATIONSHIP_EDGE_TYPE,
  adaptGraph,
  type GraphFlowEdge,
  type GraphFlowNode,
} from "./graphAdapter";
import {
  computeSeedPositions,
  createSimNodes,
  isParked,
  type SimNode,
  settle,
  stepSim,
} from "./graphLayout";
import styles from "./graph.module.css";

// Stable at module scope — reactflow warns (and re-instantiates node renderers)
// if these objects change identity between renders.
const NODE_TYPES = { [ENTITY_NODE_TYPE]: GraphNode };
const EDGE_TYPES = { [RELATIONSHIP_EDGE_TYPE]: GraphEdge };

// Positions are node *centres* (matches the layout's coordinate semantics and
// the handoff's centre-based seeds), not top-left corners.
const NODE_ORIGIN: [number, number] = [0.5, 0.5];
const FIT_VIEW_OPTIONS = { padding: 0.2 };

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
 * pan/zoom, deterministic generated layout, and the handoff's restore-to-seed
 * force sim driven by a *parked* rAF loop — it runs only while a drag is in
 * flight or the layout still has energy, then stops (0% idle CPU).
 *
 * Motion is gated: under `prefers-reduced-motion` the loop is never started —
 * nodes render at their settled seed positions and a drag simply leaves them
 * where dropped. Selection is the URL (`?entity=`): the open drilldown and the
 * node's green ring share one source of truth.
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

  // Built once. Graph.tsx remounts this component (key=source) on a source
  // switch, so the mutable sim ref can never outlive its graph.
  const builtRef = useRef<Built | null>(null);
  if (builtRef.current === null) builtRef.current = build(graph);
  const built = builtRef.current;

  const [nodes, setNodes, onNodesChange] = useNodesState(built.nodes);
  const [edges, , onEdgesChange] = useEdgesState(built.edges);

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

  return (
    <div
      className={styles.canvas}
      role="region"
      aria-label="Knowledge graph"
      data-graph-loop={loopState}
      data-graph-frames={frames}
      data-reduced-motion={reducedMotion ? "true" : "false"}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
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
      </ReactFlow>
    </div>
  );
}
