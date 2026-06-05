// Pure GET /api/graph -> reactflow nodes/edges adapter (PR-17a).
//
// The contract change in ADR 0011 is honoured here: a node's catastrophic
// alarm is driven by `pii_level === "catastrophic"` (the full level rides
// along as `piiLevel` for the PR-17b non-catastrophic halo), and an edge's
// on-path highlight is decided by `canonical_path_rank === 1` — NEVER by a
// directional source->target key, which silently mismatches because the
// canonical-path walk is undirected (the prototype's pathEdgeKey footgun).
//
// reactflow types are TYPE-ONLY imports (erased at build), so this module
// stays free of the reactflow runtime — only the dynamic GraphCanvas chunk
// pulls reactflow itself.

import type { Edge, Node } from "reactflow";

import type { GraphEdgeData } from "@/components/kit/graph/GraphEdge";
import type { GraphNodeData } from "@/components/kit/graph/GraphNode";
import type { Cardinality, PiiLevel } from "@/lib/types/meta";
import type {
  GraphEdge as ApiGraphEdge,
  GraphNode as ApiGraphNode,
  GraphResponse,
} from "@/lib/types/graph";

/** Node data carried on the reactflow node: the kit primitive's fields plus
 *  the full live PII level (the kit node only reads `catastrophic`; `piiLevel`
 *  drives the PR-17b non-catastrophic halo without a second source of truth). */
export type GraphFlowNodeData = GraphNodeData & { piiLevel: PiiLevel };

/** Edge data: the kit primitive's fields plus the declared-only cardinality. */
export type GraphFlowEdgeData = GraphEdgeData & { cardinality: Cardinality | null };

export type GraphFlowNode = Node<GraphFlowNodeData>;
export type GraphFlowEdge = Edge<GraphFlowEdgeData>;

/** reactflow `type` keys registered in GraphCanvas's nodeTypes/edgeTypes. */
export const ENTITY_NODE_TYPE = "entity";
export const RELATIONSHIP_EDGE_TYPE = "relationship";

export function adaptNode(node: ApiGraphNode): GraphFlowNode {
  return {
    id: node.id,
    type: ENTITY_NODE_TYPE,
    // Filled by the layout pass; reactflow requires a position at construction.
    position: { x: 0, y: 0 },
    data: {
      label: node.label,
      group: node.group,
      catastrophic: node.pii_level === "catastrophic",
      rowCount: node.row_count,
      piiLevel: node.pii_level,
    },
  };
}

export function adaptEdge(edge: ApiGraphEdge): GraphFlowEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: RELATIONSHIP_EDGE_TYPE,
    data: {
      declared: edge.evidence === "declared",
      // Direction-safe: rank 1 IS the primary canonical path. Never a
      // `${source}->${target}` key (the walk is undirected; storage
      // direction can oppose traversal and silently drop the highlight).
      highlighted: edge.canonical_path_rank === 1,
      cardinality: edge.cardinality,
    },
  };
}

export interface AdaptedGraph {
  nodes: GraphFlowNode[];
  edges: GraphFlowEdge[];
}

/** Map the whole projection. Positions are zeroed here and assigned by the
 *  layout pass; an empty projection yields empty arrays (a valid graph). */
export function adaptGraph(graph: GraphResponse): AdaptedGraph {
  return {
    nodes: graph.nodes.map(adaptNode),
    edges: graph.edges.map(adaptEdge),
  };
}
