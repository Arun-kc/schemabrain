// Deterministic graph layout + the parked restore-to-seed simulation (PR-17a).
//
// The design handoff hand-placed 12 demo entities in a `SEED` map and ran a
// gentle restore-to-seed sim on top. A real source returns N entities with no
// positions (the /api/graph contract is layout-free by design, ADR 0010), and
// hardcoding coordinates per entity name does not survive real data — so this
// module GENERATES deterministic seed positions from the graph's own structure
// (a horizontal canonical-path backbone with fanned satellites), then runs the
// SAME restore-to-seed sim the handoff used for the "breathing" settle + drag.
//
// Two properties are load-bearing and unit-tested:
//   1. DETERMINISM — same input always yields the same positions (no
//      Math.random / Date), so visual-regression baselines are stable and the
//      reduced-motion path can render final positions with no loop.
//   2. PARKING — the sim's total energy decays below PARK_ENERGY within a
//      bounded number of steps and stays there (no perpetual rAF, ~0% idle
//      CPU). The frame-count guard in GraphCanvas asserts the loop stops.

/** Logical canvas the seed layout targets (the handoff's coordinate space). */
export const CANVAS_W = 1000;
export const CANVAS_H = 680;

// Sim constants, ported verbatim from the handoff `stepSim`.
const SPRING = 0.026; // pull back toward the generated seed
const DAMP = 0.82; // velocity damping per step
const V_MAX = 9; // per-axis velocity clamp
const REPEL_DIST = 96; // overlap repulsion engages below this gap
const REPEL_K = 7000; // repulsion strength
const REPEL_MIN = 34; // distance floor so the force can't explode
const PAD_X = 64;
const PAD_Y = 60;

/** Below this total energy (sum of |vx|+|vy|) the layout is at rest. */
export const PARK_ENERGY = 0.6;
/** Steps run before first paint so an overlapping seed settles silently. */
export const PRE_SETTLE_STEPS = 80;

// Seed-layout geometry.
const BACKBONE_Y = CANVAS_H * 0.42;
const BACKBONE_X0 = CANVAS_W * 0.16;
const BACKBONE_X1 = CANVAS_W * 0.84;
const SATELLITE_TIER_GAP = 104; // vertical gap per fan tier (> REPEL_DIST so a
// tier-1 satellite starts just outside its anchor's overlap radius → the
// freshly-loaded layout begins near rest and the loop parks in a few steps)
const SATELLITE_X_JITTER = 22; // deterministic horizontal stagger per tier
const GOLDEN_ANGLE = 2.399963229728653; // radians (137.5°) for the no-spine spiral

export interface Vec2 {
  x: number;
  y: number;
}

export interface LayoutInput {
  /** Node ids (any order; sorted internally for determinism). */
  nodeIds: readonly string[];
  /** Undirected relationships as [source, target] id pairs. */
  edges: readonly (readonly [string, string])[];
  /** Ordered canonical-path node ids (the diameter spine); may be empty. */
  spine: readonly string[];
}

function clampToCanvas(p: Vec2): Vec2 {
  return {
    x: Math.max(PAD_X, Math.min(CANVAS_W - PAD_X, p.x)),
    y: Math.max(PAD_Y, Math.min(CANVAS_H - PAD_Y, p.y)),
  };
}

function buildAdjacency(
  nodeIds: readonly string[],
  edges: readonly (readonly [string, string])[],
): Map<string, Set<string>> {
  const adj = new Map<string, Set<string>>();
  for (const id of nodeIds) adj.set(id, new Set());
  for (const [a, b] of edges) {
    if (a === b) continue; // self-loops do not place
    adj.get(a)?.add(b);
    adj.get(b)?.add(a);
  }
  return adj;
}

/** Multi-source BFS from every spine node → each node's nearest spine anchor.
 *  Processes neighbours in sorted order so ties break deterministically. */
function nearestSpineByNode(
  adj: Map<string, Set<string>>,
  spineSet: Set<string>,
): Map<string, string> {
  const nearest = new Map<string, string>();
  let frontier: string[] = [];
  for (const id of [...spineSet].sort()) {
    nearest.set(id, id);
    frontier.push(id);
  }
  while (frontier.length > 0) {
    const next: string[] = [];
    for (const id of frontier) {
      const anchor = nearest.get(id)!;
      for (const nb of [...(adj.get(id) ?? [])].sort()) {
        if (!nearest.has(nb)) {
          nearest.set(nb, anchor);
          next.push(nb);
        }
      }
    }
    frontier = next;
  }
  return nearest;
}

/** Phyllotaxis spiral fallback when there is no canonical-path spine to anchor
 *  on (single-table or fully-disconnected schema). Deterministic, even, and
 *  centred; the sim breathes around it. */
function spiralSeeds(sortedIds: readonly string[]): Map<string, Vec2> {
  const out = new Map<string, Vec2>();
  const cx = CANVAS_W / 2;
  const cy = CANVAS_H / 2;
  const maxR = Math.min(CANVAS_W, CANVAS_H) / 2 - PAD_Y;
  const n = sortedIds.length;
  sortedIds.forEach((id, i) => {
    if (n === 1) {
      out.set(id, { x: cx, y: cy });
      return;
    }
    const r = maxR * Math.sqrt((i + 0.5) / n);
    const theta = i * GOLDEN_ANGLE;
    out.set(id, clampToCanvas({ x: cx + r * Math.cos(theta), y: cy + r * Math.sin(theta) }));
  });
  return out;
}

/**
 * Compute deterministic seed positions for the graph.
 *
 * With a canonical-path spine: lay the spine along a centred horizontal
 * backbone (the diameter reads left-to-right, the green path traces straight
 * through), then fan each off-spine node above/below its graph-nearest spine
 * anchor in deterministic tiers; fully-disconnected nodes drop to a bottom row.
 * Without a spine: a phyllotaxis spiral. Output is always within the canvas.
 */
export function computeSeedPositions(input: LayoutInput): Map<string, Vec2> {
  const sortedIds = [...input.nodeIds].sort();
  const spine = input.spine.filter((id) => input.nodeIds.includes(id));

  if (spine.length === 0) {
    return spiralSeeds(sortedIds);
  }

  const seeds = new Map<string, Vec2>();
  const spineSet = new Set(spine);

  // 1. Spine → horizontal backbone, evenly spaced in canonical order.
  spine.forEach((id, i) => {
    const t = spine.length === 1 ? 0.5 : i / (spine.length - 1);
    seeds.set(id, { x: BACKBONE_X0 + t * (BACKBONE_X1 - BACKBONE_X0), y: BACKBONE_Y });
  });

  // 2. Off-spine nodes → fanned around their nearest spine anchor.
  const adj = buildAdjacency(input.nodeIds, input.edges);
  const nearest = nearestSpineByNode(adj, spineSet);
  const satellitesByAnchor = new Map<string, string[]>();
  const floating: string[] = [];
  for (const id of sortedIds) {
    if (spineSet.has(id)) continue;
    const anchor = nearest.get(id);
    if (anchor === undefined) {
      floating.push(id); // unreachable from the spine
    } else {
      const list = satellitesByAnchor.get(anchor) ?? [];
      list.push(id);
      satellitesByAnchor.set(anchor, list);
    }
  }

  for (const [anchor, sats] of satellitesByAnchor) {
    const base = seeds.get(anchor)!;
    sats.forEach((id, k) => {
      const tier = Math.floor(k / 2) + 1; // 1,1,2,2,3,3,...
      const side = k % 2 === 0 ? -1 : 1; // above, then below
      const xShift = (tier % 2 === 0 ? 1 : -1) * tier * SATELLITE_X_JITTER;
      seeds.set(
        id,
        clampToCanvas({ x: base.x + xShift, y: base.y + side * tier * SATELLITE_TIER_GAP }),
      );
    });
  }

  // 3. Disconnected nodes → deterministic bottom row.
  floating.forEach((id, i) => {
    const t = floating.length === 1 ? 0.5 : i / (floating.length - 1);
    seeds.set(id, clampToCanvas({ x: BACKBONE_X0 + t * (BACKBONE_X1 - BACKBONE_X0), y: CANVAS_H * 0.85 }));
  });

  return seeds;
}

/** A mutable simulation particle. `sx/sy` is the immovable seed the spring
 *  restores toward; `fx/fy` pins the node during a drag (null = free). */
export interface SimNode {
  id: string;
  x: number;
  y: number;
  sx: number;
  sy: number;
  vx: number;
  vy: number;
  fx: number | null;
  fy: number | null;
}

/** Build sim particles starting at their seed positions (so a freshly-loaded,
 *  non-overlapping layout begins at rest and the loop parks immediately). */
export function createSimNodes(seeds: Map<string, Vec2>): SimNode[] {
  return [...seeds.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([id, p]) => ({ id, x: p.x, y: p.y, sx: p.x, sy: p.y, vx: 0, vy: 0, fx: null, fy: null }));
}

/**
 * Advance the simulation one step; returns the total energy (Σ |vx|+|vy|).
 * Overlap-only repulsion (engages under REPEL_DIST) + a restore-to-seed spring
 * + damping + velocity/position clamps. A pinned node (fx/fy set) holds its
 * position with zero velocity. Ported from the handoff `stepSim`.
 */
export function stepSim(nodes: SimNode[]): number {
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i];
      const b = nodes[j];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const d = Math.hypot(dx, dy) || 1;
      if (d < REPEL_DIST) {
        const f = REPEL_K / Math.max(d, REPEL_MIN) ** 2;
        const ux = dx / d;
        const uy = dy / d;
        a.vx += ux * f;
        a.vy += uy * f;
        b.vx -= ux * f;
        b.vy -= uy * f;
      }
    }
  }

  let energy = 0;
  for (const n of nodes) {
    n.vx += (n.sx - n.x) * SPRING;
    n.vy += (n.sy - n.y) * SPRING;
    n.vx *= DAMP;
    n.vy *= DAMP;
    n.vx = Math.max(-V_MAX, Math.min(V_MAX, n.vx));
    n.vy = Math.max(-V_MAX, Math.min(V_MAX, n.vy));
    if (n.fx != null && n.fy != null) {
      n.x = n.fx;
      n.y = n.fy;
      n.vx = 0;
      n.vy = 0;
    } else {
      n.x += n.vx;
      n.y += n.vy;
      n.x = Math.max(PAD_X, Math.min(CANVAS_W - PAD_X, n.x));
      n.y = Math.max(PAD_Y, Math.min(CANVAS_H - PAD_Y, n.y));
    }
    energy += Math.abs(n.vx) + Math.abs(n.vy);
  }
  return energy;
}

/** Run the sim to rest (or `maxSteps`), returning the steps actually taken.
 *  Used for the silent pre-paint settle and unit assertions. */
export function settle(nodes: SimNode[], maxSteps = PRE_SETTLE_STEPS): number {
  for (let step = 1; step <= maxSteps; step++) {
    const energy = stepSim(nodes);
    if (energy < PARK_ENERGY) return step;
  }
  return maxSteps;
}

export function isParked(energy: number): boolean {
  return energy < PARK_ENERGY;
}
