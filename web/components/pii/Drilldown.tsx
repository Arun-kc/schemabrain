"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { PIICategory, Sensitivity } from "@/lib/types";
import { TrustBadge } from "./TrustBadge";
import { HalfCircleIcon } from "../icons/HalfCircleIcon";
import styles from "./ledger.module.css";

/**
 * Drilldown for the currently selected entity. Per the Boardroom
 * Brief redesign, this is a bottom-aligned bordered card (not a
 * right-rail). When no entity is selected, the card is omitted —
 * the matrix above is the surface; the drilldown is an optional
 * companion.
 *
 * The fetch is lazy — only fires when an entity is selected, so the
 * initial matrix render isn't gated on a column request.
 */

interface DrilldownProps {
  entityName: string | null;
  sourceId?: string;
  catastrophicCategories: readonly string[];
}

export function Drilldown({
  entityName,
  sourceId,
  catastrophicCategories,
}: DrilldownProps) {
  if (entityName === null) return null;
  return (
    <section className={styles.drilldown} aria-live="polite">
      <p className={styles.drilldownLabel}>
        selected <strong>{entityName}</strong>
      </p>
      <DrilldownCard
        entityName={entityName}
        sourceId={sourceId}
        catastrophicSet={new Set(catastrophicCategories)}
      />
    </section>
  );
}

function DrilldownCard({
  entityName,
  sourceId,
  catastrophicSet,
}: {
  entityName: string;
  sourceId?: string;
  catastrophicSet: Set<string>;
}) {
  const query = useQuery({
    queryKey: ["entity-columns", entityName, sourceId],
    queryFn: () => api.entityColumns(entityName, sourceId),
  });

  if (query.isPending) {
    return (
      <div className={styles.drilldownCard}>
        <p className={styles.skeleton}>loading columns…</p>
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className={styles.drilldownCard}>
        <p className={styles.drilldownHeaderMeta} style={{ color: "var(--color-signal-red)" }}>
          {query.error.message}
        </p>
      </div>
    );
  }

  const { entity, columns } = query.data;

  return (
    <div className={styles.drilldownCard}>
      <header className={styles.drilldownHeader}>
        <div className={styles.drilldownHeaderLeft}>
          <TrustBadge
            inferenceMethod={entity.inference_method}
            validationState={entity.validation_state}
            size="md"
            showTag
          />
          <span className={styles.drilldownHeaderMeta}>
            {entity.qualified_table} · {entity.inference_method.replace(/_/g, " ")} ·{" "}
            {entity.validation_state}
          </span>
        </div>
        <span className={styles.drilldownHeaderMeta}>
          {columns.length} {columns.length === 1 ? "column" : "columns"}
        </span>
      </header>

      <div className={styles.columnList} role="list" aria-label="column breakdown">
        {columns.map((col) => (
          <ColumnRow
            key={col.name}
            name={col.name}
            sensitivity={col.sensitivity as Sensitivity}
            categories={col.pii_categories as readonly PIICategory[]}
            catastrophicSet={catastrophicSet}
          />
        ))}
      </div>

      {columns.length === 0 && (
        <p className={styles.drilldownHeaderMeta} style={{ marginTop: "1rem" }}>
          No columns indexed for this entity. The classifier hasn’t finished, or the
          source has no introspectable schema for it.
        </p>
      )}
    </div>
  );
}

function ColumnRow({
  name,
  sensitivity,
  categories,
  catastrophicSet,
}: {
  name: string;
  sensitivity: Sensitivity;
  categories: readonly PIICategory[];
  catastrophicSet: Set<string>;
}) {
  return (
    <>
      <span className={styles.colName} role="listitem">
        {name}
      </span>
      <span className={styles.colType} aria-hidden="true">
        ·
      </span>
      <span className={styles.colClass}>[{sensitivity}]</span>
      <span className={styles.colCategories}>
        {categories.length === 0 ? (
          <span aria-hidden="true">·</span>
        ) : (
          categories.map((cat, i) => (
            <span key={cat}>
              <span className={cn(catastrophicSet.has(cat) && styles.catastrophic)}>
                {catastrophicSet.has(cat) && (
                  <HalfCircleIcon className="text-(--color-signal-red) w-3.5 h-3.5 mr-1 align-middle inline-block" />
                )}
                {cat}
              </span>
              {i < categories.length - 1 && ", "}
            </span>
          ))
        )}
      </span>
    </>
  );
}
