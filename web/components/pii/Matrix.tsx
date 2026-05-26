"use client";

import { useQuery } from "@tanstack/react-query";
import { useState, Fragment } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSourceId } from "@/lib/useSourceId";
import type { PiiMatrixEntity } from "@/lib/types";
import { Drilldown } from "./Drilldown";
import { HalfCircleIcon } from "../icons/HalfCircleIcon";
import styles from "./ledger.module.css";

/**
 * The Ledger v2 — Direction A · Boardroom Brief.
 *
 * Single-table composition: entity row labels on the left, then 3
 * catastrophic columns, then a vertical-rule + padding boundary,
 * then 9 PII columns. Row alignment is intrinsic to the table.
 *
 * `sourceId` is optional: when omitted, the Matrix resolves it via
 * `useSourceId()` from /api/meta. Tests can pass an explicit value
 * to pin a specific source without hitting the meta endpoint.
 */

interface MatrixProps {
  sourceId?: string;
}

export function Matrix({ sourceId: sourceIdProp }: MatrixProps) {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const sourceId = sourceIdProp ?? resolvedSourceId ?? undefined;

  const matrixQuery = useQuery({
    queryKey: ["pii-matrix", sourceId],
    queryFn: () => api.piiMatrix(sourceId),
    enabled: sourceIdProp !== undefined || sourceStatus !== "loading",
  });

  const [selectedEntity, setSelectedEntity] = useState<string | null>(null);

  if (matrixQuery.isPending) {
    return (
      <div className={styles.shell}>
        <p className={styles.skeleton}>loading matrix…</p>
      </div>
    );
  }

  if (matrixQuery.isError) {
    return <MatrixError message={matrixQuery.error.message} />;
  }

  const data = matrixQuery.data;
  const catastrophicCategories = data.catastrophic_categories;
  const catastrophicSet = new Set(catastrophicCategories);
  const piiCategories = data.categories.filter((c) => !catastrophicSet.has(c));

  return (
    <div className={styles.shell}>
      <TitleStrip
        catastrophicCount={data.totals.catastrophic_columns}
        entityCount={data.totals.entities}
      />

      <div className={styles.hero}>
        <Slab
          totals={data.totals}
          catastrophicCategories={catastrophicCategories}
        />

        <div className={styles.matrixWrap}>
          <table className={styles.matrix} role="grid" aria-label="pii ledger matrix">
            <thead>
              <tr className={styles.groupRow}>
                <th scope="col" />
                <th
                  scope="colgroup"
                  colSpan={catastrophicCategories.length}
                  className={styles.groupCatastrophic}
                >
                  catastrophic
                </th>
                <th
                  scope="colgroup"
                  colSpan={piiCategories.length}
                  className={styles.groupPii}
                >
                  pii
                </th>
              </tr>
              <tr className={styles.headRow}>
                <th scope="col" className={styles.entityHeader}>
                  entity
                </th>
                {catastrophicCategories.map((cat, i) => (
                  <th
                    key={cat}
                    scope="col"
                    className={cn(
                      styles.catastrophicHeader,
                      styles.headerCell,
                      i === 0 && styles.boundaryStart,
                    )}
                    aria-label={cat}
                  >
                    <span className={styles.headerLabel}>{abbreviateCategory(cat)}</span>
                    <span className={styles.tooltip}>{cat.replace(/_/g, " ")}</span>
                  </th>
                ))}
                {piiCategories.map((cat, i) => (
                  <th
                    key={cat}
                    scope="col"
                    className={cn(
                      styles.headerCell,
                      i === 0 && styles.boundaryStart
                    )}
                    aria-label={cat}
                  >
                    <span className={styles.headerLabel}>{abbreviateCategory(cat)}</span>
                    <span className={styles.tooltip}>{cat.replace(/_/g, " ")}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.entities.map((entity) => (
                <EntityRow
                  key={entity.name}
                  entity={entity}
                  catastrophicCategories={catastrophicCategories}
                  piiCategories={piiCategories}
                  isSelected={selectedEntity === entity.name}
                  onSelect={() =>
                    setSelectedEntity(selectedEntity === entity.name ? null : entity.name)
                  }
                />
              ))}
            </tbody>
          </table>
          <p className={styles.matrixCaption}>
            <span className={styles.glyph}>◐</span> catastrophic-leak column · click row
            to drill in
          </p>
        </div>
      </div>

      <Drilldown
        entityName={selectedEntity}
        sourceId={sourceId}
        catastrophicCategories={catastrophicCategories}
      />
    </div>
  );
}

/* ─────────── title strip ─────────── */

function TitleStrip({
  catastrophicCount,
  entityCount,
}: {
  catastrophicCount: number;
  entityCount: number;
}) {
  const summary =
    catastrophicCount === 0
      ? "no catastrophic exposure"
      : `${catastrophicCount} catastrophic across ${entityCount} ${entityCount === 1 ? "entity" : "entities"}`;
  return (
    <div className={styles.titleStrip}>
      <h1 className={styles.surfaceTitle}>The Ledger</h1>
      <p className={styles.titleMeta}>{summary}</p>
    </div>
  );
}

/* ─────────── stat slab ─────────── */

interface SidecarTotals {
  entities: number;
  columns: number;
  catastrophic_columns: number;
  pii_columns: number;
  confidential_columns: number;
  internal_or_public_columns: number;
}

function Slab({
  totals,
  catastrophicCategories,
}: {
  totals: SidecarTotals;
  catastrophicCategories: readonly string[];
}) {
  const hasCatastrophic = totals.catastrophic_columns > 0;
  return (
    <aside className={styles.slab} aria-label="ledger summary">
      <p className={styles.slabLabel}>catastrophic exposure</p>
      <div className={styles.slabNumberWrap} data-danger={hasCatastrophic}>
        <span className={styles.slabNumber} data-danger={hasCatastrophic}>{totals.catastrophic_columns}</span>
      </div>
      <p className={styles.slabSublabel}>
        {hasCatastrophic
          ? `cols across ${totals.entities} ${totals.entities === 1 ? "entity" : "entities"}`
          : "the firewall has nothing to refuse yet"}
      </p>
      <ul className={styles.slabCategories} aria-label="catastrophic categories">
        {catastrophicCategories.map((cat) => (
          <li key={cat} className="flex items-center gap-1.5 py-0.5">
            <HalfCircleIcon className="text-(--color-signal-red) w-3.5 h-3.5 shrink-0" />
            <span>{cat.replace(/_/g, " ")}</span>
          </li>
        ))}
      </ul>

      <dl className={styles.slabBreakdown}>
        <dt className={styles.slabBreakdownNumber}>{totals.pii_columns}</dt>
        <dd className={styles.slabBreakdownLabel}>pii cols</dd>
        <dt className={styles.slabBreakdownNumber}>{totals.confidential_columns}</dt>
        <dd className={styles.slabBreakdownLabel}>confidential cols</dd>
        <dt className={styles.slabBreakdownNumber}>{totals.internal_or_public_columns}</dt>
        <dd className={styles.slabBreakdownLabel}>internal/public cols</dd>
      </dl>

      <dl className={styles.slabFooter}>
        <dt className={styles.slabBreakdownNumber}>{totals.entities}</dt>
        <dd className={styles.slabBreakdownLabel}>
          {totals.entities === 1 ? "entity" : "entities"}
        </dd>
        <dt className={styles.slabBreakdownNumber}>{totals.columns}</dt>
        <dd className={styles.slabBreakdownLabel}>columns total</dd>
      </dl>
    </aside>
  );
}

/* ─────────── entity row ─────────── */

interface EntityRowProps {
  entity: PiiMatrixEntity;
  catastrophicCategories: readonly string[];
  piiCategories: readonly string[];
  isSelected: boolean;
  onSelect: () => void;
}

function EntityRow({
  entity,
  catastrophicCategories,
  piiCategories,
  isSelected,
  onSelect,
}: EntityRowProps) {
  return (
    <tr
      className={cn(isSelected && styles.selected)}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      tabIndex={0}
      aria-selected={isSelected}
    >
      <td className={styles.entityCell}>
        <span className={styles.entityName}>{entity.name}</span>
        <span className={styles.entityTable}>{entity.qualified_table}</span>
      </td>
      {catastrophicCategories.map((cat, i) => (
        <DataCell
          key={cat}
          count={entity.counts[cat] ?? 0}
          isCatastrophic
          entityName={entity.name}
          category={cat}
          atBoundary={i === 0}
        />
      ))}
      {piiCategories.map((cat, i) => (
        <DataCell
          key={cat}
          count={entity.counts[cat] ?? 0}
          isCatastrophic={false}
          entityName={entity.name}
          category={cat}
          atBoundary={i === 0}
        />
      ))}
    </tr>
  );
}

function DataCell({
  count,
  isCatastrophic,
  entityName,
  category,
  atBoundary,
}: {
  count: number;
  isCatastrophic: boolean;
  entityName: string;
  category: string;
  atBoundary: boolean;
}) {
  const boundaryClass = atBoundary ? styles.boundaryStart : "";
  if (count === 0) {
    return (
      <td
        className={cn(boundaryClass)}
        aria-label={`${entityName} · ${category}: zero`}
      >
        <span className={styles.zero} aria-hidden="true">
          ·
        </span>
      </td>
    );
  }
  if (isCatastrophic) {
    return (
      <td
        className={cn(styles.catastrophicCell, boundaryClass)}
        aria-label={`${entityName} · ${category}: ${count} catastrophic`}
      >
        <span className={styles.catastrophicBadge}>
          <span className={styles.catastrophicGlyph}>
            <HalfCircleIcon className="text-(--color-signal-red) w-3 h-3 mr-1" />
            {count}
          </span>
        </span>
      </td>
    );
  }
  return (
    <td
      className={cn(boundaryClass)}
      aria-label={`${entityName} · ${category}: ${count} ${count === 1 ? "column" : "columns"}`}
    >
      <span className={styles.piiBadge}>{count}</span>
    </td>
  );
}

/* ─────────── error state ─────────── */

function MatrixError({ message }: { message: string }) {
  const isNoSource =
    message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <div className={styles.shell}>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>The Ledger</h1>
        <p className={styles.titleMeta}>
          {isNoSource ? "status — no source connected" : "status — error"}
        </p>
      </div>
      <div className={styles.errorCard}>
        <span className={styles.errorIcon} aria-hidden="true">
          {isNoSource ? (
            <svg className="w-6 h-6 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
            </svg>
          ) : (
            <svg className="w-6 h-6 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          )}
        </span>
        <div className={styles.errorBody}>
          <h2>{isNoSource ? "The ledger needs a source." : "Matrix unavailable."}</h2>
          {isNoSource ? (
            <Fragment>
              <p>
                SchemaBrain hasn’t been pointed at a database yet. Once a source is
                indexed, this surface fills in entity-by-entity as the classifier runs.
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

/* ─────────── category abbreviations ─────────── */

function abbreviateCategory(cat: string): string {
  const map: Record<string, string> = {
    contact: "contact",
    financial: "fin",
    payment_card: "pmt",
    health: "hlth",
    genetic: "gen",
    biometric: "bio",
    behavioral: "bhv",
    online_identifier: "oid",
    credential: "crd",
    government_id: "gov",
    location: "loc",
    demographic_protected: "dem",
  };
  return map[cat] ?? cat;
}
