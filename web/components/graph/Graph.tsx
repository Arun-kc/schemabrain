"use client";

import dynamic from "next/dynamic";
import { Suspense, useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { useGraph } from "@/lib/useGraph";
import type { GraphCanvasProps } from "./GraphCanvas";
import styles from "./graph.module.css";

// reactflow is heavy and DOM-only — load the canvas client-side, after the
// shell paints, so its chunk (and reactflow) never enters the server build or
// the landing bundle.
const GraphCanvas = dynamic<GraphCanvasProps>(() => import("./GraphCanvas"), {
  ssr: false,
  loading: () => <CanvasState>summoning the canvas…</CanvasState>,
});

/**
 * Knowledge-graph surface (/graph) — the signature read-only map of the
 * schema: one node per entity, edges for canonical joins, the canonical path
 * traced through, and catastrophic-PII entities flagged. Mounted inside the
 * app shell (the shell owns the chrome + the global `?entity=` drilldown).
 *
 * Selection lives in the URL (ADR 0005): clicking a node pushes
 * `?entity=<id>`, which the shell's DrilldownSheet reads to slide open; the
 * same param drives the node's green ring. PR-17a ships the canvas, layout,
 * and motion gate — the floating tools/legend/path/minimap chrome and the
 * designed indexing state are PR-17b.
 */
function GraphInner() {
  const { graph, status } = useGraph();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const selectedId = params.get("entity");

  const openEntity = useCallback(
    (id: string) => {
      router.push(`${pathname}?entity=${encodeURIComponent(id)}`);
    },
    [router, pathname],
  );

  const clearSelection = useCallback(() => {
    if (params.get("entity")) router.replace(pathname);
  }, [router, pathname, params]);

  if (status === "loading") {
    return <CanvasState>mapping the schema…</CanvasState>;
  }
  if (status === "error") {
    return <GraphError />;
  }
  if (graph === null || graph.nodes.length === 0) {
    return <GraphEmpty />;
  }

  return (
    <div className={styles.root}>
      <GraphCanvas
        key={graph.source_connection_id}
        graph={graph}
        selectedId={selectedId}
        onOpenEntity={openEntity}
        onClearSelection={clearSelection}
      />
    </div>
  );
}

export function Graph() {
  // useSearchParams requires a Suspense boundary under `output: "export"`.
  return (
    <Suspense fallback={<CanvasState>mapping the schema…</CanvasState>}>
      <GraphInner />
    </Suspense>
  );
}

function CanvasState({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.root}>
      <div className={styles.state}>
        <p className={styles.skeleton}>{children}</p>
      </div>
    </div>
  );
}

function GraphEmpty() {
  return (
    <div className={styles.root}>
      <div className={styles.state}>
        <p className={styles.stateTitle}>No graph yet</p>
        <p className={styles.stateBody}>
          The knowledge graph appears once a source is indexed and its business entities are bound.
          Run <code>schemabrain init</code> then <code>schemabrain index</code> to map this schema.
        </p>
      </div>
    </div>
  );
}

function GraphError() {
  return (
    <div className={styles.root}>
      <div className={styles.state}>
        <p className={styles.stateTitle}>Couldn’t load the graph</p>
        <p className={styles.stateBody}>
          The graph needs an indexed source. Run <code>schemabrain init</code> then{" "}
          <code>schemabrain index</code>, and make sure the dashboard sidecar is running.
        </p>
      </div>
    </div>
  );
}
