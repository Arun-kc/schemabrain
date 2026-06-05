import { describe, expect, it } from "vitest";

import {
  CANVAS_H,
  CANVAS_W,
  PARK_ENERGY,
  computeSeedPositions,
  createSimNodes,
  isParked,
  settle,
  stepSim,
  type LayoutInput,
} from "./graphLayout";

const PAD_X = 64;
const PAD_Y = 60;

// A small SaaS-shaped graph: a 4-node spine + two satellites + one
// disconnected node.
const SAMPLE: LayoutInput = {
  nodeIds: ["order_item", "order", "user", "tenant", "payment", "session", "lonely"],
  edges: [
    ["order_item", "order"],
    ["order", "user"],
    ["user", "tenant"],
    ["order", "payment"],
    ["user", "session"],
  ],
  spine: ["order_item", "order", "user", "tenant"],
};

function within(p: { x: number; y: number }): boolean {
  return p.x >= PAD_X && p.x <= CANVAS_W - PAD_X && p.y >= PAD_Y && p.y <= CANVAS_H - PAD_Y;
}

describe("computeSeedPositions", () => {
  it("is deterministic — identical input yields identical positions", () => {
    const a = computeSeedPositions(SAMPLE);
    const b = computeSeedPositions(SAMPLE);
    expect([...a.entries()]).toEqual([...b.entries()]);
  });

  it("places a position for every node, all within the canvas", () => {
    const seeds = computeSeedPositions(SAMPLE);
    expect(seeds.size).toBe(SAMPLE.nodeIds.length);
    for (const p of seeds.values()) expect(within(p)).toBe(true);
  });

  it("lays the spine along a single horizontal backbone in canonical order", () => {
    const seeds = computeSeedPositions(SAMPLE);
    const ys = SAMPLE.spine.map((id) => seeds.get(id)!.y);
    expect(new Set(ys).size).toBe(1); // all on one backbone row
    const xs = SAMPLE.spine.map((id) => seeds.get(id)!.x);
    // strictly increasing x in canonical order
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeGreaterThan(xs[i - 1]);
  });

  it("fans satellites off the backbone row", () => {
    const seeds = computeSeedPositions(SAMPLE);
    const backboneY = seeds.get("order")!.y;
    expect(seeds.get("payment")!.y).not.toBe(backboneY); // off-spine satellite
    expect(seeds.get("session")!.y).not.toBe(backboneY);
  });

  it("drops a disconnected node to the bottom row, not the backbone", () => {
    const seeds = computeSeedPositions(SAMPLE);
    expect(seeds.get("lonely")!.y).toBeGreaterThan(seeds.get("order")!.y);
  });

  it("falls back to a centred spiral when there is no spine", () => {
    const seeds = computeSeedPositions({
      nodeIds: ["a", "b", "c", "d", "e"],
      edges: [],
      spine: [],
    });
    expect(seeds.size).toBe(5);
    for (const p of seeds.values()) expect(within(p)).toBe(true);
    // distinct positions (no stacking)
    const keys = new Set([...seeds.values()].map((p) => `${p.x.toFixed(2)},${p.y.toFixed(2)}`));
    expect(keys.size).toBe(5);
  });

  it("centres a single node", () => {
    const seeds = computeSeedPositions({ nodeIds: ["solo"], edges: [], spine: [] });
    expect(seeds.get("solo")).toEqual({ x: CANVAS_W / 2, y: CANVAS_H / 2 });
  });

  it("yields an empty map for an empty graph", () => {
    expect(computeSeedPositions({ nodeIds: [], edges: [], spine: [] }).size).toBe(0);
  });
});

describe("createSimNodes", () => {
  it("starts each particle at its seed, at rest, unpinned, id-sorted", () => {
    const nodes = createSimNodes(computeSeedPositions(SAMPLE));
    expect(nodes.map((n) => n.id)).toEqual([...SAMPLE.nodeIds].sort());
    for (const n of nodes) {
      expect(n.x).toBe(n.sx);
      expect(n.y).toBe(n.sy);
      expect(n.vx).toBe(0);
      expect(n.vy).toBe(0);
      expect(n.fx).toBeNull();
    }
  });
});

describe("stepSim — the parked simulation", () => {
  it("parks: energy decays below threshold within a bounded number of steps", () => {
    const nodes = createSimNodes(computeSeedPositions(SAMPLE));
    // Perturb a node far from its seed (as a drag-release would).
    nodes[0].x += 220;
    nodes[0].y -= 140;
    const steps = settle(nodes, 400);
    expect(steps).toBeLessThan(400); // it actually reached rest, not the cap
    expect(isParked(stepSim(nodes))).toBe(true); // and stays parked
  });

  it("a freshly-loaded layout parks almost immediately (~0% idle CPU)", () => {
    const nodes = createSimNodes(computeSeedPositions(SAMPLE));
    const steps = settle(nodes, 200);
    expect(steps).toBeLessThan(60);
  });

  it("returns the cap when it cannot reach rest within maxSteps", () => {
    const nodes = createSimNodes(computeSeedPositions(SAMPLE));
    nodes[0].x += 220; // perturbed, but only one step allowed → can't park yet
    expect(settle(nodes, 1)).toBe(1);
  });

  it("is deterministic — two identical sims step to identical state", () => {
    const a = createSimNodes(computeSeedPositions(SAMPLE));
    const b = createSimNodes(computeSeedPositions(SAMPLE));
    a[0].x += 100;
    b[0].x += 100;
    for (let i = 0; i < 50; i++) {
      stepSim(a);
      stepSim(b);
    }
    expect(a.map((n) => [n.x, n.y, n.vx, n.vy])).toEqual(b.map((n) => [n.x, n.y, n.vx, n.vy]));
  });

  it("holds a pinned node fixed with zero velocity", () => {
    const nodes = createSimNodes(computeSeedPositions(SAMPLE));
    const pin = nodes[2];
    pin.fx = 300;
    pin.fy = 200;
    for (let i = 0; i < 10; i++) stepSim(nodes);
    expect(pin.x).toBe(300);
    expect(pin.y).toBe(200);
    expect(pin.vx).toBe(0);
    expect(pin.vy).toBe(0);
  });

  it("keeps every node within the canvas while settling", () => {
    const nodes = createSimNodes(computeSeedPositions(SAMPLE));
    nodes.forEach((n) => {
      n.x += 500; // shove everything hard against the edge
    });
    for (let i = 0; i < 100; i++) stepSim(nodes);
    for (const n of nodes) expect(within(n)).toBe(true);
  });
});
