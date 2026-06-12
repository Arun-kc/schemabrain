"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { cardinalityLabel, groupLabel } from "@/lib/entities";
import { serializeDictionary } from "@/lib/dict/serialize";
import { categoryLabel, sensitivityLabel } from "@/lib/dict/labels";
import {
  categoryChipKind,
  DICT_GROUP_ORDER,
  entityDotVar,
  leadCategory,
  piiColumns,
} from "@/lib/dict/present";
import { useSourceId } from "@/lib/useSourceId";
import type { DictColumn, DictEntity, DictionaryModel, DictJoin } from "@/lib/types";
import { Button, Icon, PiiChip, useToast } from "@/components/kit";
import styles from "./dict.module.css";

/**
 * THE DICTIONARY — the generated, browsable data dictionary (/dict), mounted
 * inside the app shell. A left index (grouped + searchable) drives a right
 * entry panel: the entity's description, PII chips, columns with meaning,
 * semantic joins (with the server-rendered ON clause + log-mined flag), and
 * the metrics it anchors. "Export Markdown" re-renders the WHOLE dictionary
 * client-side via `serializeDictionary` — byte-for-byte equal to the
 * `schemabrain docs` CLI golden. Source-scoped and read-only.
 */
export function DataDictionary() {
  const { sourceId, status: sourceStatus } = useSourceId();
  const query = useQuery({
    queryKey: ["dict", sourceId],
    queryFn: () => api.dict(sourceId ?? undefined),
    enabled: sourceStatus !== "loading",
  });

  if (sourceStatus === "loading" || query.isPending) {
    return (
      <div className={styles.shell}>
        <Hero meta="reading the dictionary…" />
        <p className={styles.skeleton} role="status">
          loading data dictionary…
        </p>
      </div>
    );
  }

  if (query.isError) {
    return <ErrorView message={query.error.message} />;
  }

  return <DictionaryView model={query.data} />;
}

/* ─────────── loaded view ─────────── */

function DictionaryView({ model }: { model: DictionaryModel }) {
  const entities = useMemo(
    () => model.sources.flatMap((source) => source.entities),
    [model.sources],
  );
  const [search, setSearch] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return entities;
    return entities.filter((entity) => entity.name.toLowerCase().includes(needle));
  }, [entities, search]);

  // The selected entity, defaulting to the first one (overall, so a search
  // that hides it does not blank the panel).
  const selected =
    entities.find((entity) => entity.name === selectedName) ?? entities[0] ?? null;
  const meta = `${entities.length} ${entities.length === 1 ? "entity" : "entities"}`;

  if (entities.length === 0) {
    return (
      <div className={styles.shell}>
        <Hero meta="empty" />
        <EmptyState />
      </div>
    );
  }

  return (
    <div className={styles.shell}>
      <Hero meta={meta} />
      <div className={styles.grid}>
        <EntityIndex
          entities={filtered}
          search={search}
          onSearch={setSearch}
          selectedName={selected?.name ?? null}
          onSelect={setSelectedName}
        />
        {selected ? <EntityEntry entity={selected} model={model} /> : <NoMatch />}
      </div>
    </div>
  );
}

/* ─────────── hero ─────────── */

function Hero({ meta }: { meta: string }) {
  return (
    <>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>Data dictionary</h1>
        <p className={styles.titleMeta}>{meta}</p>
      </div>
      <p className={styles.lede}>
        A living dictionary generated from the local store — every entity&apos;s description,
        columns, semantic joins, and PII flags. Export the whole thing to Markdown for your repo or
        wiki; it matches the <code>schemabrain docs</code> CLI byte for byte.
      </p>
    </>
  );
}

/* ─────────── left index ─────────── */

interface EntityIndexProps {
  entities: readonly DictEntity[];
  search: string;
  onSearch: (value: string) => void;
  selectedName: string | null;
  onSelect: (name: string) => void;
}

function EntityIndex({ entities, search, onSearch, selectedName, onSelect }: EntityIndexProps) {
  return (
    <aside className={styles.index} aria-label="Entity index">
      <div className={styles.search}>
        <Icon name="search" size={14} />
        <input
          type="search"
          placeholder="Find entity…"
          value={search}
          onChange={(event) => onSearch(event.target.value)}
          aria-label="Filter entities by name"
        />
      </div>
      <div className={styles.idxScroll}>
        {DICT_GROUP_ORDER.map((group) => {
          const items = entities.filter((entity) => entity.group === group);
          if (items.length === 0) return null;
          return (
            <div key={group}>
              <div className={styles.idxGroup}>{groupLabel(group)}</div>
              {items.map((entity) => {
                const dot = entityDotVar(entity);
                return (
                  <button
                    type="button"
                    key={entity.name}
                    className={cn(styles.idxItem, entity.name === selectedName && styles.idxItemOn)}
                    aria-current={entity.name === selectedName ? "true" : undefined}
                    onClick={() => onSelect(entity.name)}
                  >
                    {entity.name}
                    {dot && (
                      <span className={styles.idxDot} style={{ background: dot }} aria-hidden />
                    )}
                  </button>
                );
              })}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

function NoMatch() {
  return (
    <section className={styles.entry}>
      <div className={styles.body}>
        <p className={styles.muted}>No entity matches that search.</p>
      </div>
    </section>
  );
}

/* ─────────── right entry ─────────── */

function EntityEntry({ entity, model }: { entity: DictEntity; model: DictionaryModel }) {
  const flagColumns = piiColumns(entity);
  return (
    <section className={styles.entry} aria-label={`${entity.name} dictionary entry`}>
      <div className={styles.entryHead}>
        <div className={styles.entryRow1}>
          <PiiChip kind="green" dot>
            {groupLabel(entity.group)}
          </PiiChip>
          <h2 className={styles.entryName}>{entity.name}</h2>
          <span className={styles.entryMeta}>
            {entity.qualified_table} · identity {entity.identity}
          </span>
          <div className={styles.export}>
            <ExportButton model={model} />
          </div>
        </div>
        <p className={styles.desc}>{entity.description || "No description."}</p>
        <div className={styles.flags}>
          {flagColumns.length > 0 ? (
            flagColumns.map((column) => <ColumnFlag key={column.name} column={column} />)
          ) : (
            <PiiChip kind="none">No PII columns</PiiChip>
          )}
        </div>
      </div>

      <div className={styles.body}>
        <ColumnsSection columns={entity.columns} />
        {entity.joins.length > 0 && <JoinsSection joins={entity.joins} />}
        {entity.metrics.length > 0 && (
          <MetricsSection metrics={entity.metrics.map((metric) => metric.name)} />
        )}
      </div>
    </section>
  );
}

/** The lead category chip for a PII column (its highest-severity tag). */
function ColumnFlag({ column }: { column: DictColumn }) {
  const lead = leadCategory(column);
  if (!lead) return null;
  return <PiiChip kind={categoryChipKind(lead)} dot>{categoryLabel(lead)}</PiiChip>;
}

function ColumnsSection({ columns }: { columns: readonly DictColumn[] }) {
  return (
    <div className={styles.sec}>
      <h3 className={styles.secHead}>
        <Icon name="table-2" size={13} /> Columns
        <span className={styles.secCount}>{columns.length}</span>
      </h3>
      <table className={styles.colTable}>
        <caption className={styles.srOnly}>Columns with type, sensitivity, and meaning.</caption>
        <tbody>
          {columns.map((column) => (
            <tr key={column.name}>
              <td className={styles.colName}>
                {column.name}
                <span className={styles.colType}>{column.data_type}</span>
              </td>
              <td className={styles.colPii}>
                {column.pii_categories.length > 0 ? (
                  <PiiChip kind={categoryChipKind(leadCategory(column)!)} dot>
                    {categoryLabel(leadCategory(column)!)}
                  </PiiChip>
                ) : (
                  <span className={styles.muted}>{sensitivityLabel(column.pii_sensitivity)}</span>
                )}
              </td>
              <td className={styles.colMeaning}>
                {column.description || <span className={styles.muted}>—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function JoinsSection({ joins }: { joins: readonly DictJoin[] }) {
  return (
    <div className={styles.sec}>
      <h3 className={styles.secHead}>
        <Icon name="git-merge" size={13} /> Joins
        <span className={styles.secCount}>{joins.length}</span>
      </h3>
      {joins.map((join) => {
        const mined = join.provenance.toLowerCase().includes("log");
        const card = cardinalityLabel(join.cardinality);
        return (
          <div key={join.name} className={styles.join}>
            <span className={styles.joinName}>
              {join.source_entity} → {join.target_entity}
            </span>
            <code className={styles.joinOn}>{join.on_clause}</code>
            <span className={cn(styles.joinCard, mined && styles.joinMined)}>
              {mined ? "log-mined" : join.provenance}
              {card ? ` · ${card}` : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function MetricsSection({ metrics }: { metrics: readonly string[] }) {
  return (
    <div className={styles.sec}>
      <h3 className={styles.secHead}>
        <Icon name="sparkles" size={13} /> Metrics defined here
      </h3>
      <div className={styles.metricList}>
        {metrics.map((name) => (
          <PiiChip key={name} kind="green" dot>
            {name}
          </PiiChip>
        ))}
      </div>
    </div>
  );
}

/* ─────────── export ─────────── */

function ExportButton({ model }: { model: DictionaryModel }) {
  const { copyToClipboard } = useToast();
  const [copied, setCopied] = useState(false);

  const onExport = async () => {
    await copyToClipboard(serializeDictionary(model), "markdown");
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1700);
  };

  return (
    <Button type="button" onClick={onExport}>
      <Icon name={copied ? "check" : "copy"} size={13} />
      {copied ? " Copied markdown" : " Export Markdown"}
    </Button>
  );
}

/* ─────────── states ─────────── */

function EmptyState() {
  return (
    <div className={styles.emptyState}>
      <span className={styles.emptyIcon}>
        <Icon name="book-open" size={28} label="no entities" />
      </span>
      <p className={styles.emptyTitle}>No entities yet</p>
      <p className={styles.emptyText}>
        This source has no curated entities. Run <code>schemabrain index</code> and apply an entity
        pack, then the dictionary fills in automatically.
      </p>
    </div>
  );
}

function ErrorView({ message }: { message: string }) {
  return (
    <div className={styles.shell}>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>Data dictionary</h1>
      </div>
      <div className={styles.errorCard} role="alert">
        <span className={styles.errorIcon}>
          <Icon name="x" size={20} label="error" />
        </span>
        <div className={styles.errorBody}>
          <h2>Could not load the dictionary</h2>
          <p>{message}</p>
          <code>schemabrain index &lt;your-database-url&gt;</code>
        </div>
      </div>
    </div>
  );
}
