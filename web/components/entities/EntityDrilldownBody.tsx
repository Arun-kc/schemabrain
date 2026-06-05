"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import {
  cardinalityLabel,
  categoryLabel,
  columnIsCatastrophic,
  confidenceDisplay,
  formatRowCount,
  groupChipKind,
  groupLabel,
  topSimilarColumns,
} from "@/lib/entities";
import { categoryChipKind } from "@/lib/refusals";
import { isCatastrophic } from "@/lib/types";
import type {
  EntityColumn,
  EntityDrilldownHeader,
  EntityJoin,
  EntityMetric,
} from "@/lib/types";
import { Button, Eyebrow, Icon, IconButton, PiiChip, useToast } from "@/components/kit";
import { useSourceId } from "@/lib/useSourceId";
import styles from "./entitySheet.module.css";

/**
 * The BODY of the app-level entity drilldown sheet (wsENT). The shell owns the
 * container — mount, slide, scrim, focus-trap, `?entity=` addressing (ADR 0005);
 * this renders the rich header + physical/semantic panes into it. Faithful port
 * of the design handoff `entity.jsx`, adapted to the real engine contract:
 *
 *   - bind confidence is CATEGORICAL (a labelled pill), never a numeric meter;
 *   - "similar columns" are structured nearest neighbours + cosine score, with
 *     no free-text reason (the route omits it to avoid leaking a value);
 *   - row counts render "—" when unknown, never a fabricated 0;
 *   - the catastrophic floor is gated via the data-model category enum
 *     (`isCatastrophic` / `categoryChipKind`), not the chip-kind footgun.
 *
 * Read-only: the footer "View in PII matrix" navigates, and the join "copy"
 * writes to the clipboard — neither issues a write to the sidecar.
 */

const SIMILAR_LIMIT = 6;

interface EntityDrilldownBodyProps {
  /** The `?entity=` value — the entity NAME (a graph node id). */
  entity: string;
  onClose: () => void;
}

export function EntityDrilldownBody({ entity, onClose }: EntityDrilldownBodyProps) {
  const router = useRouter();
  const { sourceId, status: sourceStatus } = useSourceId();
  const query = useQuery({
    // Once the source resolves, this key matches the /pii bottom-card
    // drilldown's (`["entity-columns", name, sourceId ?? undefined]`) so the two
    // share one TanStack cache entry. Normalise null→undefined so they coincide,
    // and gate on the source so the query never fires with a null id.
    queryKey: ["entity-columns", entity, sourceId ?? undefined],
    queryFn: () => api.entityColumns(entity, sourceId ?? undefined),
    enabled: sourceStatus !== "loading",
  });

  return (
    <>
      <header className="sb-drill-head">
        <IconButton
          className="sb-drill-close"
          icon="x"
          label="Close entity detail"
          onClick={onClose}
        />
        {query.data ? <RichHeader header={query.data.entity} /> : <h2>{entity}</h2>}
      </header>

      {query.isPending && (
        <div className="sb-drill-body">
          <p className={styles.skeleton} role="status">
            loading entity…
          </p>
        </div>
      )}

      {query.isError && (
        <div className="sb-drill-body">
          <ErrorNote message={query.error.message} />
        </div>
      )}

      {query.data && (
        <>
          <div className="sb-drill-body">
            <PhysicalSection columns={query.data.columns} />
            <SemanticSection
              metrics={query.data.metrics}
              joins={query.data.joins}
              columns={query.data.columns}
              referencedBy={query.data.referenced_by}
            />
          </div>
          <footer className="sb-drill-foot">
            <Button
              variant="primary"
              icon="shield"
              className={styles.footPrimary}
              onClick={() => router.push(`/pii?focus=${encodeURIComponent(entity)}`)}
            >
              View in PII matrix
            </Button>
          </footer>
        </>
      )}
    </>
  );
}

/* ─────────── header ─────────── */

function RichHeader({ header }: { header: EntityDrilldownHeader }) {
  const confidence = confidenceDisplay(header.confidence);
  return (
    <>
      <div className={styles.top}>
        <PiiChip kind={groupChipKind(header.group)} dot>
          {groupLabel(header.group)}
        </PiiChip>
        <h2>{header.name}</h2>
      </div>
      <div className={styles.meta}>
        <Eyebrow className={styles.metaRows}>{formatRowCount(header.rows)} rows</Eyebrow>
        {confidence && (
          <span className={styles.metaConf}>
            <Eyebrow>bind confidence</Eyebrow>
            <PiiChip kind={confidence.kind}>{confidence.label}</PiiChip>
          </span>
        )}
      </div>
      {header.rationale && (
        <div className={styles.rationale}>
          <div className={styles.rationaleLab}>
            <Icon name="sparkles" size={12} /> Binding rationale
          </div>
          {header.rationale}
        </div>
      )}
    </>
  );
}

/* ─────────── physical pane ─────────── */

function PhysicalSection({ columns }: { columns: readonly EntityColumn[] }) {
  return (
    <section className={styles.sec}>
      <div className={styles.secHead}>
        <Icon name="table-2" size={14} className={styles.secIconInk} />
        <Eyebrow className={styles.secEye}>Physical · columns</Eyebrow>
        <span className={styles.secCount}>
          {columns.length} {columns.length === 1 ? "col" : "cols"}
        </span>
      </div>
      {columns.map((column) => (
        <ColumnRow key={column.name} column={column} />
      ))}
      {columns.length === 0 && <p className={styles.empty}>No columns indexed for this entity.</p>}
    </section>
  );
}

function ColumnRow({ column }: { column: EntityColumn }) {
  const categories = column.pii_categories;
  const catastrophic = columnIsCatastrophic(categories);
  return (
    <div className={cn(styles.col, catastrophic && styles.colCat)}>
      <div className={styles.colMain}>
        <span className={styles.colName}>{column.name}</span>{" "}
        <span className={styles.colType}>{column.data_type}</span>
        {column.description && <div className={styles.colMeaning}>{column.description}</div>}
      </div>
      <div className={styles.colChips}>
        {categories.length === 0 ? (
          <PiiChip kind="none">—</PiiChip>
        ) : (
          categories.map((category) => (
            <PiiChip key={category} kind={categoryChipKind(category)} dot>
              {categoryLabel(category)}
              {isCatastrophic(category) ? " · CATASTROPHIC" : ""}
            </PiiChip>
          ))
        )}
      </div>
    </div>
  );
}

/* ─────────── semantic pane ─────────── */

function SemanticSection({
  metrics,
  joins,
  columns,
  referencedBy,
}: {
  metrics: readonly EntityMetric[];
  joins: readonly EntityJoin[];
  columns: readonly EntityColumn[];
  referencedBy: { metrics: number; joins: number };
}) {
  const { shown, hidden } = topSimilarColumns(columns, SIMILAR_LIMIT);
  return (
    <section className={styles.sec}>
      <div className={styles.secHead}>
        <Icon name="sparkles" size={14} className={styles.secIconCyan} />
        <Eyebrow className={styles.secEye}>Semantic · meaning</Eyebrow>
        <span className={styles.secCount}>
          referenced by {referencedBy.metrics} {referencedBy.metrics === 1 ? "metric" : "metrics"} ·{" "}
          {referencedBy.joins} {referencedBy.joins === 1 ? "join" : "joins"}
        </span>
      </div>

      {metrics.length > 0 && (
        <div className={styles.subBlock}>
          <Eyebrow className={styles.subEye}>Metrics defined here</Eyebrow>
          <div className={styles.metricTags}>
            {metrics.map((metric) => (
              <PiiChip key={metric.name} kind="green" dot>
                {metric.name}
              </PiiChip>
            ))}
          </div>
        </div>
      )}

      <Eyebrow className={styles.subEye}>Canonical joins · paste-ready</Eyebrow>
      {joins.length === 0 ? (
        <p className={styles.empty}>No canonical joins reference this entity.</p>
      ) : (
        joins.map((join) => <JoinCard key={join.name} join={join} />)
      )}

      {shown.length > 0 && (
        <div className={styles.simBlock}>
          <Eyebrow className={styles.subEye}>Similar columns · embeddings</Eyebrow>
          {shown.map((pair) => (
            <div
              key={`${pair.source}~${pair.schema}.${pair.table}.${pair.column}`}
              className={styles.sim}
            >
              <span className={styles.simIcon}>
                <Icon name="radar" size={16} />
              </span>
              <div className={styles.simText}>
                <div>
                  <span className={styles.simCol}>
                    {pair.schema}.{pair.table}.{pair.column}
                  </span>
                  <span className={styles.simScore}>{pair.score.toFixed(2)}</span>
                </div>
                <div className={styles.simSource}>near {pair.source}</div>
              </div>
            </div>
          ))}
          {hidden > 0 && (
            <p className={styles.simMore}>
              +{hidden} more similar {hidden === 1 ? "column" : "columns"}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function JoinCard({ join }: { join: EntityJoin }) {
  const { copyToClipboard } = useToast();
  // on_clause is the bare predicate; prefix ON so the copied text is paste-ready.
  const clause = `ON ${join.on_clause}`;
  const card = cardinalityLabel(join.cardinality);
  return (
    <div className={styles.join}>
      <div className={styles.joinHead}>
        <Icon name="git-merge" size={13} className={styles.joinHeadIcon} />
        <span className={styles.joinPair}>
          {join.source_entity} → {join.target_entity}
        </span>
        {join.is_log_mined ? (
          <span className={styles.joinMined}>log-mined{card ? ` · ${card}` : ""}</span>
        ) : (
          card && <span className={styles.joinCard}>{card}</span>
        )}
      </div>
      <div className={styles.joinOn}>
        <code className={styles.joinOnCode}>{clause}</code>
        <button
          type="button"
          className={styles.joinCopy}
          aria-label={`Copy ON clause for ${join.name}`}
          onClick={() => copyToClipboard(clause, "join copied")}
        >
          <Icon name="copy" size={14} />
        </button>
      </div>
    </div>
  );
}

/* ─────────── error ─────────── */

function ErrorNote({ message }: { message: string }) {
  const isNotFound = message.includes("404") || message.toLowerCase().includes("not found");
  return (
    <p className={styles.errorText} role="alert">
      {isNotFound
        ? "This entity isn’t bound in the current source."
        : `Couldn’t load this entity. ${message}`}
    </p>
  );
}
