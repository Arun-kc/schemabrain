import { describe, expect, it } from "vitest";

import type { GraphResponse } from "@/lib/types/graph";

import {
  ENTITY_NODE_TYPE,
  RELATIONSHIP_EDGE_TYPE,
  adaptEdge,
  adaptGraph,
  adaptNode,
} from "./graphAdapter";

function apiNode(over: Partial<GraphResponse["nodes"][number]> = {}): GraphResponse["nodes"][number] {
  return {
    id: "user",
    label: "user",
    group: "identity",
    pii_level: "none",
    row_count: null,
    refusal_count: 0,
    ...over,
  };
}

function apiEdge(over: Partial<GraphResponse["edges"][number]> = {}): GraphResponse["edges"][number] {
  return {
    id: "orders_user_id",
    source: "order",
    target: "user",
    evidence: "declared",
    canonical_path_rank: 0,
    cardinality: null,
    ...over,
  };
}

describe("adaptNode", () => {
  it("maps catastrophic alarm off pii_level === 'catastrophic'", () => {
    const n = adaptNode(apiNode({ pii_level: "catastrophic" }));
    expect(n.data.catastrophic).toBe(true);
    expect(n.data.piiLevel).toBe("catastrophic");
  });

  it("carries the non-catastrophic tier without flagging the alarm", () => {
    const n = adaptNode(apiNode({ pii_level: "pii" }));
    expect(n.data.catastrophic).toBe(false);
    expect(n.data.piiLevel).toBe("pii"); // drives the PR-17b lighter halo
  });

  it("preserves a null row_count (never fabricates 0) and the group", () => {
    const n = adaptNode(apiNode({ group: "billing", row_count: null }));
    expect(n.data.rowCount).toBeNull();
    expect(n.data.group).toBe("billing");
  });

  it("carries the live refusal_count through for the hotspot overlay", () => {
    expect(adaptNode(apiNode({ refusal_count: 4 })).data.refusalCount).toBe(4);
    expect(adaptNode(apiNode({ refusal_count: 0 })).data.refusalCount).toBe(0);
  });

  it("emits the entity node type with a placeholder position for the layout pass", () => {
    const n = adaptNode(apiNode());
    expect(n.type).toBe(ENTITY_NODE_TYPE);
    expect(n.position).toEqual({ x: 0, y: 0 });
    expect(n.id).toBe("user");
  });
});

describe("adaptEdge", () => {
  it("marks declared FK edges and dashes the rest", () => {
    expect(adaptEdge(apiEdge({ evidence: "declared" })).data!.declared).toBe(true);
    expect(adaptEdge(apiEdge({ evidence: "log_mined" })).data!.declared).toBe(false);
    expect(adaptEdge(apiEdge({ evidence: "inferred" })).data!.declared).toBe(false);
  });

  it("highlights ONLY the primary canonical path (rank 1), not rank 0 or 2", () => {
    expect(adaptEdge(apiEdge({ canonical_path_rank: 1 })).data!.highlighted).toBe(true);
    expect(adaptEdge(apiEdge({ canonical_path_rank: 0 })).data!.highlighted).toBe(false);
    // rank 2 is a reserved alternate — never the primary green spine.
    expect(adaptEdge(apiEdge({ canonical_path_rank: 2 })).data!.highlighted).toBe(false);
  });

  it("passes cardinality through, including null for mined/inferred edges", () => {
    expect(adaptEdge(apiEdge({ cardinality: "many_to_one" })).data!.cardinality).toBe("many_to_one");
    expect(adaptEdge(apiEdge({ evidence: "log_mined", cardinality: null })).data!.cardinality).toBeNull();
  });

  it("emits the relationship edge type with the wire source/target", () => {
    const e = adaptEdge(apiEdge());
    expect(e.type).toBe(RELATIONSHIP_EDGE_TYPE);
    expect(e.source).toBe("order");
    expect(e.target).toBe("user");
  });
});

describe("adaptGraph", () => {
  it("maps every node and edge", () => {
    const graph: GraphResponse = {
      source_connection_id: "s",
      nodes: [apiNode({ id: "user" }), apiNode({ id: "order", group: "activity" })],
      edges: [apiEdge()],
      canonical_path: { nodes: [], edges: [], hops: 0 },
      unattributed_refusals: 0,
    };
    const out = adaptGraph(graph);
    expect(out.nodes.map((n) => n.id)).toEqual(["user", "order"]);
    expect(out.edges).toHaveLength(1);
  });

  it("yields empty arrays for an empty projection", () => {
    const out = adaptGraph({
      source_connection_id: "s",
      nodes: [],
      edges: [],
      canonical_path: { nodes: [], edges: [], hops: 0 },
      unattributed_refusals: 0,
    });
    expect(out.nodes).toEqual([]);
    expect(out.edges).toEqual([]);
  });
});
