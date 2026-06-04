"use client";

import { useQuery } from "@tanstack/react-query";
import { Fragment, Suspense, useState } from "react";
import { api } from "@/lib/api";
import { useSourceId } from "@/lib/useSourceId";
import type { PIICategory, PiiMatrixColumn } from "@/lib/types";
import { type MatrixSummary, qualifiedColumn, summarizeMatrix } from "@/lib/piiMatrix";
import { Drilldown } from "./Drilldown";
import { MatrixGrid } from "./MatrixGrid";
import styles from "./ledger.module.css";

/**
 * THE READOUT — the PII confidence heatmap surface (/pii), mounted inside the
 * app shell (the shell owns the chrome: top bar, nav rail, source selector,
 * theme). One row per classified column × 12 category columns, intensity from
 * the column's single advisory band (ADR 0009 — no fabricated per-category
 * score). Read-only: the editable enforcement policy lives on /policy.
 *
 * `sourceId` is optional: when omitted, the surface resolves it via
 * `useSourceId()` from /api/meta. Tests can pin an explicit value.
 */
interface MatrixProps {
  sourceId?: string;
}

interface Selection {
  entity: string;
  col: string;
}

export function Matrix({ sourceId: sourceIdProp }: MatrixProps) {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const sourceId = sourceIdProp ?? resolvedSourceId ?? undefined;

  const matrixQuery = useQuery({
    queryKey: ["pii-matrix", sourceId],
    queryFn: () => api.piiMatrix(sourceId),
    enabled: sourceIdProp !== undefined || sourceStatus !== "loading",
  });

  // Selection drives the entity Drilldown below the grid; tracked here (outside
  // the grid's Suspense boundary) so the drilldown can read it. Re-clicking the
  // same column closes the drilldown.
  const [selected, setSelected] = useState<Selection | null>(null);
  const handleSelect = (col: PiiMatrixColumn) => {
    const qc = qualifiedColumn(col);
    setSelected((prev) => (prev?.col === qc ? null : { entity: col.entity, col: qc }));
  };

  if (matrixQuery.isPending) {
    return (
      <div className={styles.shell}>
        <p className={styles.skeleton}>loading the matrix…</p>
      </div>
    );
  }

  if (matrixQuery.isError) {
    return <MatrixError message={matrixQuery.error.message} />;
  }

  const data = matrixQuery.data;
  const summary = summarizeMatrix(data.columns);

  return (
    <div className={styles.shell}>
      <TitleStrip blocked={summary.blocked} columns={summary.total} />
      <SummaryStrip summary={summary} categoryCount={data.categories.length} />

      <Suspense fallback={<p className={styles.skeleton}>loading columns…</p>}>
        <MatrixGrid
          columns={data.columns}
          categories={data.categories as readonly PIICategory[]}
          catastrophicCategories={data.catastrophic_categories}
          selectedColumn={selected?.col ?? null}
          onSelect={handleSelect}
        />
      </Suspense>

      <Drilldown
        entityName={selected?.entity ?? null}
        sourceId={sourceId}
        catastrophicCategories={data.catastrophic_categories}
      />
    </div>
  );
}

/* ─────────── title strip ─────────── */

function TitleStrip({ blocked, columns }: { blocked: number; columns: number }) {
  const meta =
    blocked === 0
      ? "no floor-locked columns"
      : `${blocked} floor-locked across ${columns} ${columns === 1 ? "column" : "columns"}`;
  return (
    <div className={styles.titleStrip}>
      <h1 className={styles.surfaceTitle}>PII matrix</h1>
      <p className={styles.titleMeta}>{meta}</p>
    </div>
  );
}

/* ─────────── summary strip ─────────── */

function SummaryStrip({
  summary,
  categoryCount,
}: {
  summary: MatrixSummary;
  categoryCount: number;
}) {
  return (
    <div className={styles.summaryStrip} aria-label="pii disposition summary">
      <Stat n={summary.blocked} label="blocked" tone="alarm" />
      <Stat n={summary.redacted} label="redact" tone="cyan" />
      <Stat n={summary.allowed} label="allow" tone="green" />
      <Stat n={summary.total} label={summary.total === 1 ? "column" : "columns"} tone="ink" sub={`${categoryCount} categories`} />
    </div>
  );
}

function Stat({
  n,
  label,
  tone,
  sub,
}: {
  n: number;
  label: string;
  tone: "alarm" | "cyan" | "green" | "ink";
  sub?: string;
}) {
  return (
    <div className={styles.stat}>
      <span className={styles.statNum} data-tone={tone}>
        {n}
      </span>
      <span className={styles.statLabel}>{label}</span>
      {sub && <span className={styles.statSub}>{sub}</span>}
    </div>
  );
}

/* ─────────── error state ─────────── */

function MatrixError({ message }: { message: string }) {
  const isNoSource =
    message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <div className={styles.shell}>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>PII matrix</h1>
        <p className={styles.titleMeta}>
          {isNoSource ? "status — no source connected" : "status — error"}
        </p>
      </div>
      <div className={styles.errorCard}>
        <span className={styles.errorIcon} aria-hidden="true">
          {isNoSource ? (
            <svg className="w-6 h-6 text-(--amber)" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
            </svg>
          ) : (
            <svg className="w-6 h-6 text-(--alarm)" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          )}
        </span>
        <div className={styles.errorBody}>
          <h2>{isNoSource ? "The matrix needs a source." : "Matrix unavailable."}</h2>
          {isNoSource ? (
            <Fragment>
              <p>
                SchemaBrain hasn’t been pointed at a database yet. Once a source is
                indexed, this surface fills in column-by-column as the classifier runs.
              </p>
              <code>$ schemabrain init</code>
              <code>$ schemabrain index --target postgres://your-replica</code>
              <p>Then refresh. Indexing 50 entities typically takes ~90 seconds.</p>
            </Fragment>
          ) : (
            <Fragment>
              <p>{message}</p>
              <p>
                The sidecar is running, but the matrix query failed. Check the sidecar
                logs in your terminal for the underlying cause.
              </p>
            </Fragment>
          )}
        </div>
      </div>
    </div>
  );
}
