"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { PiiMatrixEntity } from "@/lib/types";
import { TrustBadge } from "./TrustBadge";
import { Drilldown } from "./Drilldown";
import styles from "./ledger.module.css";

interface MatrixProps {
  sourceId?: string;
}

/**
 * The Ledger surface — entity × PII-category matrix + drill-down rail.
 *
 * Architecture:
 *   - One round-trip to /api/entities/pii-matrix fills the matrix.
 *   - Click on an entity row selects it; drill-down rail fetches
 *     /api/entities/{name}/columns lazily and renders per-column chips.
 *   - Catastrophic categories get a red column-header underline; rows
 *     that carry any catastrophic column get a red gutter tick; chips
 *     for catastrophic counts use the stamp clip-path treatment.
 *   - Empty-state branch fires when totals.catastrophic_columns === 0.
 */
export function Matrix({ sourceId }: MatrixProps) {
  const matrixQuery = useQuery({
    queryKey: ["pii-matrix", sourceId],
    queryFn: () => api.piiMatrix(sourceId),
  });

  // URL-sharing of the selected entity is a nice-to-have but out of D2 scope;
  // local state keeps the surface snappy and doesn't fight React Server Components.
  const [selectedEntity, setSelectedEntity] = useState<string | null>(null);

  if (matrixQuery.isPending) {
    return <MatrixSkeleton />;
  }

  if (matrixQuery.isError) {
    return <MatrixError message={matrixQuery.error.message} />;
  }

  const data = matrixQuery.data;
  const catastrophicSet = new Set(data.catastrophic_categories);
  const noCatastrophic = data.totals.catastrophic_columns === 0;

  return (
    <div className="mx-auto max-w-[88rem] px-rhythm-wide py-section">
      <Header
        sourceId={data.source_connection_id}
        entityCount={data.totals.entities}
        columnCount={data.totals.columns}
      />

      <Headline
        catastrophicCount={data.totals.catastrophic_columns}
        entityCount={data.totals.entities}
        noCatastrophic={noCatastrophic}
      />

      <hr className="mt-rhythm-wide mb-rhythm-base border-(--color-ink-300)" />

      <div className="grid grid-cols-[1fr_18rem] gap-rhythm-wide max-[1100px]:grid-cols-1">
        <div className="overflow-x-auto">
          <table className={styles.matrix} role="grid">
            <thead>
              <tr>
                <th scope="col" className={styles.entityHeader}>
                  entity
                </th>
                {data.categories.map((cat) => (
                  <th
                    key={cat}
                    scope="col"
                    className={cn(catastrophicSet.has(cat) && styles.catastrophicHeader)}
                  >
                    {abbreviateCategory(cat)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.entities.map((entity) => (
                <EntityRow
                  key={entity.name}
                  entity={entity}
                  categories={data.categories}
                  catastrophicSet={catastrophicSet}
                  isSelected={selectedEntity === entity.name}
                  onSelect={() => setSelectedEntity(entity.name)}
                />
              ))}
            </tbody>
          </table>

          <Footer totals={data.totals} />
        </div>

        <aside className="border-l border-(--color-ink-200) pl-rhythm-base max-[1100px]:border-l-0 max-[1100px]:pl-0 max-[1100px]:border-t max-[1100px]:pt-rhythm-base">
          {selectedEntity ? (
            <Drilldown entityName={selectedEntity} sourceId={sourceId} />
          ) : (
            <DrilldownPlaceholder />
          )}
        </aside>
      </div>

      {noCatastrophic && data.totals.entities > 0 && <EmptyStateAddendum />}
    </div>
  );
}

function Header({
  sourceId,
  entityCount,
  columnCount,
}: {
  sourceId: string;
  entityCount: number;
  columnCount: number;
}) {
  return (
    <header className="mb-section flex items-baseline justify-between gap-rhythm-base">
      <div>
        <p className="font-mono text-xs uppercase tracking-widest text-(--text-muted)">
          schemabrain · dashboard
        </p>
        <p className="mt-1 font-mono text-sm text-(--color-ink-700)">
          ─────────────
        </p>
      </div>
      <div className="text-right">
        <p className="font-mono text-sm text-(--color-ink-700) flex items-center gap-2 justify-end">
          <span className="h-2 w-2 rounded-full bg-(--color-signal-green)" aria-hidden />
          {sourceId}
        </p>
        <p className="font-mono text-xs text-(--text-muted) mt-1">
          {entityCount} entities · {columnCount} columns
        </p>
      </div>
    </header>
  );
}

function Headline({
  catastrophicCount,
  entityCount,
  noCatastrophic,
}: {
  catastrophicCount: number;
  entityCount: number;
  noCatastrophic: boolean;
}) {
  if (noCatastrophic) {
    return (
      <section>
        <h1
          className="font-display text-(--text-primary) leading-[1.05]"
          style={{ fontSize: "clamp(2rem, 1rem + 3vw, 4rem)" }}
        >
          No catastrophic-leak
          <br />
          columns detected.
        </h1>
        <p className="mt-rhythm-base font-mono text-sm text-(--text-muted)">
          {entityCount} {entityCount === 1 ? "entity" : "entities"} scanned · the grid is the answer
        </p>
      </section>
    );
  }
  return (
    <section>
      <h1
        className="font-display text-(--text-primary) leading-[1.05]"
        style={{ fontSize: "clamp(2rem, 1rem + 3vw, 4rem)" }}
      >
        {catastrophicCount}{" "}
        {catastrophicCount === 1 ? "column carries" : "columns carry"}
        <br />
        catastrophic-leak
        <br />
        categories.
      </h1>
      <p className="mt-rhythm-base font-mono text-sm text-(--text-muted)">
        Across {entityCount} {entityCount === 1 ? "entity" : "entities"} · click a row to drill in
      </p>
    </section>
  );
}

interface EntityRowProps {
  entity: PiiMatrixEntity;
  categories: readonly string[];
  catastrophicSet: Set<string>;
  isSelected: boolean;
  onSelect: () => void;
}

function EntityRow({
  entity,
  categories,
  catastrophicSet,
  isSelected,
  onSelect,
}: EntityRowProps) {
  return (
    <tr
      className={cn(
        entity.has_catastrophic && styles.hasCatastrophic,
        isSelected && styles.selected,
      )}
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
        <span className="inline-flex items-center gap-2">
          <TrustBadge
            inferenceMethod={entity.inference_method}
            validationState={entity.validation_state}
            size="sm"
          />
          {entity.qualified_table}
        </span>
      </td>
      {categories.map((cat) => {
        const count = entity.counts[cat] ?? 0;
        const isCatastrophic = catastrophicSet.has(cat);
        if (count === 0) {
          return (
            <td key={cat} aria-label={`${cat}: zero columns`}>
              <span className={styles.zero} aria-hidden>
                ·
              </span>
            </td>
          );
        }
        if (isCatastrophic) {
          return (
            <td
              key={cat}
              aria-label={`${cat}: ${count} catastrophic column${count === 1 ? "" : "s"}`}
            >
              <span className={styles.catastrophicChip}>{count}</span>
            </td>
          );
        }
        return (
          <td key={cat} aria-label={`${cat}: ${count} column${count === 1 ? "" : "s"}`}>
            {count}
          </td>
        );
      })}
    </tr>
  );
}

function Footer({
  totals,
}: {
  totals: {
    entities: number;
    columns: number;
    catastrophic_columns: number;
    confidential_columns: number;
    pii_columns: number;
    internal_or_public_columns: number;
  };
}) {
  return (
    <p className="mt-rhythm-wide pt-rhythm-base border-t border-(--color-ink-300) font-mono text-xs text-(--text-muted)">
      {totals.entities} entities · {totals.columns} columns · {totals.catastrophic_columns}{" "}
      catastrophic · {totals.confidential_columns} confidential · {totals.pii_columns} pii ·{" "}
      {totals.internal_or_public_columns} internal/public
    </p>
  );
}

function DrilldownPlaceholder() {
  return (
    <div className="font-mono text-xs text-(--text-muted)">
      <p className="uppercase tracking-widest">drill</p>
      <p className="mt-1 text-(--color-ink-300)">────</p>
      <p className="mt-rhythm-base">
        Click an entity row to inspect its columns + per-column PII tags.
      </p>
    </div>
  );
}

function EmptyStateAddendum() {
  return (
    <section className="mt-section border-t border-(--color-ink-200) pt-section">
      <p className="font-mono text-xs uppercase tracking-widest text-(--text-muted)">
        two paths from here
      </p>
      <ol className="mt-rhythm-base space-y-rhythm-base font-mono text-sm text-(--color-ink-700)">
        <li>
          <strong className="text-(--color-ink-900)">1.</strong> Index a richer schema —{" "}
          <code className="bg-(--surface-overlay) border border-(--color-ink-200) px-1 text-(--color-signal-green)">
            schemabrain index --target postgres://…
          </code>
          <span className="block text-(--text-muted) pl-4">
            The matrix populates entity-by-entity as it scans.
          </span>
        </li>
        <li>
          <strong className="text-(--color-ink-900)">2.</strong> Tag a column manually —{" "}
          <code className="bg-(--surface-overlay) border border-(--color-ink-200) px-1 text-(--color-signal-green)">
            schemabrain tag &lt;entity&gt; &lt;column&gt; --pii credential
          </code>
          <span className="block text-(--text-muted) pl-4">
            Useful when you want to refuse on a column the classifier missed.
          </span>
        </li>
      </ol>
    </section>
  );
}

function MatrixSkeleton() {
  return (
    <div className="mx-auto max-w-[88rem] px-rhythm-wide py-section">
      <p className="font-mono text-sm text-(--text-muted)">loading matrix…</p>
    </div>
  );
}

function MatrixError({ message }: { message: string }) {
  return (
    <div className="mx-auto max-w-[88rem] px-rhythm-wide py-section">
      <p className="font-mono text-sm text-(--color-signal-red)">
        Matrix unavailable: {message}
      </p>
      <p className="mt-rhythm-base font-mono text-xs text-(--text-muted)">
        Most common cause: no source indexed yet. Run{" "}
        <code className="bg-(--surface-overlay) border border-(--color-ink-200) px-1">
          schemabrain index --target postgres://…
        </code>{" "}
        against your source DB, then reload.
      </p>
    </div>
  );
}

/**
 * Map a long PII category name to a compact column header label.
 * The 12 names don't fit a 12-column grid at common viewport widths,
 * so we use the operator-recognizable abbreviations from RFC §5.1
 * wireframe. Full name lives in the column's aria-label.
 */
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
