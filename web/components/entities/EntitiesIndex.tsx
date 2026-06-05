"use client";

import { useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  confidenceDisplay,
  DEFAULT_ENTITY_SORT,
  type EntitySort,
  type EntitySortKey,
  formatRowCount,
  groupDotVar,
  groupLabel,
  nextSort,
  piiLevelChip,
  sortEntities,
} from "@/lib/entities";
import { useSourceId } from "@/lib/useSourceId";
import type { EntityListItem, EntityListResponse } from "@/lib/types";
import { Icon, PiiChip } from "@/components/kit";
import styles from "./entities.module.css";

/**
 * THE INDEX — the Entities table (/entities), mounted inside the app shell. The
 * sortable companion to the knowledge graph: one row per bound entity with its
 * cosmetic group, decisive PII level, categorical bind confidence, and the
 * metric/join dependency counts. Selecting a row opens the shared drilldown
 * sheet via `?entity=` (the same URL state the graph drives) so Back closes it.
 *
 * Read-only. Row counts and confidence render "—" when unknown — never a
 * fabricated 0 or band (ADR 0009 / the read-only honesty guardrails).
 */
export function EntitiesIndex() {
  const { sourceId, status: sourceStatus } = useSourceId();
  const query = useQuery({
    queryKey: ["entities", sourceId],
    queryFn: () => api.entities(sourceId ?? undefined),
    enabled: sourceStatus !== "loading",
  });

  const [sort, setSort] = useState<EntitySort>(DEFAULT_ENTITY_SORT);
  const router = useRouter();
  const pathname = usePathname();
  const open = (name: string) => router.push(`${pathname}?entity=${encodeURIComponent(name)}`);
  const onSort = (key: EntitySortKey) => setSort((current) => nextSort(current, key));

  if (sourceStatus === "loading" || query.isPending) {
    return (
      <div className={styles.shell}>
        <Hero meta="reading the catalogue…" />
        <p className={styles.skeleton} role="status">
          loading entities…
        </p>
      </div>
    );
  }

  if (query.isError) {
    return <ErrorView message={query.error.message} />;
  }

  const { items, summary } = query.data;
  const sorted = sortEntities(items, sort);
  const meta =
    summary.catastrophic_entities > 0
      ? `${summary.catastrophic_entities} on the catastrophic floor`
      : "no catastrophic exposure";

  return (
    <div className={styles.shell}>
      <Hero meta={meta} />
      {items.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          <StatStrip summary={summary} />
          <EntitiesTable rows={sorted} sort={sort} onSort={onSort} onOpen={open} />
        </>
      )}
    </div>
  );
}

/* ─────────── hero ─────────── */

function Hero({ meta }: { meta: string }) {
  return (
    <>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>Entities</h1>
        <p className={styles.titleMeta}>{meta}</p>
      </div>
      <p className={styles.lede}>
        Every business entity SchemaBrain bound out of your raw schema — with binding confidence,
        PII exposure, and how many metrics and joins depend on it. Select a row to open its
        drilldown.
      </p>
    </>
  );
}

/* ─────────── stat strip ─────────── */

function StatStrip({ summary }: { summary: EntityListResponse["summary"] }) {
  return (
    <div className={styles.summaryStrip} role="group" aria-label="entities summary">
      <Stat
        n={summary.entities}
        label={summary.entities === 1 ? "bound entity" : "bound entities"}
        tone="ink"
      />
      <Stat
        n={summary.catastrophic_entities}
        label="catastrophic PII"
        tone={summary.catastrophic_entities > 0 ? "alarm" : "ink"}
      />
      <Stat n={summary.metrics} label={summary.metrics === 1 ? "metric" : "metrics"} tone="ink" />
      <Stat n={summary.joins} label={summary.joins === 1 ? "join" : "joins"} tone="ink" />
    </div>
  );
}

function Stat({ n, label, tone }: { n: number; label: string; tone: "alarm" | "ink" }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statNum} data-tone={tone}>
        {n}
      </span>
      <span className={styles.statLabel}>{label}</span>
    </div>
  );
}

/* ─────────── table ─────────── */

function EntitiesTable({
  rows,
  sort,
  onSort,
  onOpen,
}: {
  rows: readonly EntityListItem[];
  sort: EntitySort;
  onSort: (key: EntitySortKey) => void;
  onOpen: (name: string) => void;
}) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <caption className={styles.srOnly}>Bound entities — select a column header to sort.</caption>
        <thead>
          <tr>
            <SortHeader label="Entity" sortKey="name" sort={sort} onSort={onSort} />
            <SortHeader label="Group" sortKey="group" sort={sort} onSort={onSort} />
            <SortHeader label="Rows" sortKey="rows" sort={sort} onSort={onSort} numeric />
            <SortHeader label="PII" sortKey="pii_level" sort={sort} onSort={onSort} />
            <SortHeader label="Confidence" sortKey="confidence" sort={sort} onSort={onSort} />
            <SortHeader label="Metrics" sortKey="metrics_count" sort={sort} onSort={onSort} numeric />
            <SortHeader label="Joins" sortKey="joins_count" sort={sort} onSort={onSort} numeric />
            <th className={styles.th} scope="col">
              <span className={styles.srOnly}>Open</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <EntityRow key={row.name} row={row} onOpen={onOpen} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
  numeric = false,
}: {
  label: string;
  sortKey: EntitySortKey;
  sort: EntitySort;
  onSort: (key: EntitySortKey) => void;
  numeric?: boolean;
}) {
  const active = sort.key === sortKey;
  const ariaSort = active ? (sort.dir === 1 ? "ascending" : "descending") : "none";
  return (
    <th className={cn(styles.th, numeric && styles.thNum)} aria-sort={ariaSort} scope="col">
      <button type="button" className={styles.sortBtn} onClick={() => onSort(sortKey)}>
        {label}
        {active && (
          <span className={styles.arrow} aria-hidden="true">
            {sort.dir === 1 ? "↑" : "↓"}
          </span>
        )}
      </button>
    </th>
  );
}

function EntityRow({ row, onOpen }: { row: EntityListItem; onOpen: (name: string) => void }) {
  const confidence = confidenceDisplay(row.confidence);
  const pii = piiLevelChip(row.pii_level);
  const floorOrPii = row.pii_level === "catastrophic" || row.pii_level === "pii";
  return (
    <tr className={styles.row} onClick={() => onOpen(row.name)}>
      <td>
        <button
          type="button"
          className={styles.entBtn}
          onClick={(event) => {
            event.stopPropagation();
            onOpen(row.name);
          }}
        >
          <span
            className={styles.dot}
            style={{ background: groupDotVar(row.group, row.pii_level) }}
            aria-hidden="true"
          />
          <span className={styles.entName}>{row.name}</span>
        </button>
      </td>
      <td className={styles.groupCell}>{groupLabel(row.group)}</td>
      <td className={styles.num}>{formatRowCount(row.rows)}</td>
      <td>
        <PiiChip kind={pii.kind} dot={floorOrPii}>
          {pii.label}
        </PiiChip>
      </td>
      <td>
        {confidence ? (
          <PiiChip kind={confidence.kind}>{confidence.label}</PiiChip>
        ) : (
          <span className={styles.dash}>—</span>
        )}
      </td>
      <td className={styles.num}>{row.metrics_count}</td>
      <td className={styles.num}>{row.joins_count}</td>
      <td className={styles.num}>
        <span className={styles.openArrow} aria-hidden="true">
          <Icon name="arrow-right" size={15} />
        </span>
      </td>
    </tr>
  );
}

/* ─────────── empty / error ─────────── */

function EmptyState() {
  return (
    <div className={styles.emptyState}>
      <Icon name="boxes" size={40} className={styles.emptyIcon} label="no entities" />
      <h2 className={styles.emptyTitle}>No entities bound yet.</h2>
      <p className={styles.emptyText}>
        This source is indexed, but no business entities have been bound from its schema. Run the
        wizard with entity inference enabled to populate the catalogue.
      </p>
    </div>
  );
}

function ErrorView({ message }: { message: string }) {
  const isNoSource = message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <div className={styles.shell}>
      <Hero meta={isNoSource ? "status — no source connected" : "status — error"} />
      <div className={styles.errorCard}>
        <span className={styles.errorIcon} aria-hidden="true">
          <Icon name={isNoSource ? "database" : "x"} size={22} />
        </span>
        <div className={styles.errorBody}>
          {isNoSource ? (
            <>
              <h2>Point SchemaBrain at a database.</h2>
              <p>The catalogue fills in once a source is indexed and entities are bound.</p>
              <code>$ schemabrain init</code>
              <code>$ schemabrain index --target postgres://your-replica</code>
            </>
          ) : (
            <>
              <h2>Couldn’t load the entities.</h2>
              <p>{message}</p>
              <p>
                The sidecar is running, but the entities query failed. Check the sidecar logs in
                your terminal for the underlying cause.
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
