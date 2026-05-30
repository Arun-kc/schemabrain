"use client";

import { Fragment, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useSourceId } from "@/lib/useSourceId";
import type {
  EffectiveEnforcement,
  PIICategory,
  PolicyCategoryRollup,
  PolicyColumnEntry,
  PolicyOrigin,
  PolicyResponse,
} from "@/lib/types";
import styles from "./policy.module.css";

/**
 * PolicyView — operator-editable PII policy surface.
 *
 * Consumes GET /api/pii/policy. The Ledger (Matrix) shows what tags
 * EXIST on the source; PolicyView shows what the firewall DOES with
 * those tags — the active block set, the catastrophic floor that's
 * always on, the diff preview between postures, and the per-column
 * verdict listing with operator-override provenance.
 *
 * Single-source-of-truth for visual language: matches Matrix's slab
 * + table composition, signal-red catastrophic chip, signal-amber
 * pii chip, signal-green safe chip. Verdict cells use the same
 * three-tone palette so colour means the same thing across both
 * surfaces.
 */

interface PolicyViewProps {
  sourceId?: string;
}

export function PolicyView({ sourceId: sourceIdProp }: PolicyViewProps) {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const sourceId = sourceIdProp ?? resolvedSourceId ?? undefined;

  const policyQuery = useQuery({
    queryKey: ["pii-policy", sourceId],
    queryFn: () => api.piiPolicy(sourceId),
    enabled: sourceIdProp !== undefined || sourceStatus !== "loading",
  });

  if (policyQuery.isPending) {
    return (
      <section className={styles.shell} aria-label="pii policy">
        <p className={styles.skeleton}>loading policy…</p>
      </section>
    );
  }

  if (policyQuery.isError) {
    return <PolicyError message={policyQuery.error.message} />;
  }

  return <PolicyContent data={policyQuery.data} />;
}

function PolicyContent({ data }: { data: PolicyResponse }) {
  return (
    <section className={styles.shell} aria-label="pii policy">
      <TitleStrip
        blockSource={data.block_source}
        activeBlockCount={data.active_block.length}
        policyPath={data.policy_path}
      />

      {data.yaml_parse_error && <YamlParseError error={data.yaml_parse_error} />}

      <div className={styles.posture}>
        <ActiveBlockPanel
          activeBlock={data.active_block}
          catastrophicFloor={data.catastrophic_floor}
          blockSource={data.block_source}
          policyPath={data.policy_path}
        />
        <DiffPreviewPanel diff={data.diff_preview} totalRows={data.per_column.length} />
      </div>

      <CategoryRollup
        rows={data.category_rollup}
        catastrophicFloor={data.catastrophic_floor}
      />

      <PerColumnTable rows={data.per_column} />
    </section>
  );
}

/* ─────────── title strip ─────────── */

function TitleStrip({
  blockSource,
  activeBlockCount,
  policyPath,
}: {
  blockSource: PolicyResponse["block_source"];
  activeBlockCount: number;
  policyPath: string;
}) {
  const summary =
    blockSource === "yaml"
      ? `${activeBlockCount} ${activeBlockCount === 1 ? "category" : "categories"} blocked via ${policyPath}`
      : `${activeBlockCount} ${activeBlockCount === 1 ? "category" : "categories"} blocked · catastrophic floor only`;
  return (
    <div className={styles.titleStrip}>
      <h2 className={styles.surfaceTitle}>Active Policy</h2>
      <p className={styles.titleMeta}>{summary}</p>
    </div>
  );
}

/* ─────────── YAML parse error alert ─────────── */

function YamlParseError({ error }: { error: string }) {
  return (
    <div className={styles.parseError} role="alert">
      <span className={styles.parseErrorLabel}>pii_policy.yaml failed to parse</span>
      <span className={styles.parseErrorBody}>
        falling back to catastrophic floor · {error}
      </span>
    </div>
  );
}

/* ─────────── posture panels ─────────── */

function ActiveBlockPanel({
  activeBlock,
  catastrophicFloor,
  blockSource,
  policyPath,
}: {
  activeBlock: readonly PIICategory[];
  catastrophicFloor: readonly PIICategory[];
  blockSource: PolicyResponse["block_source"];
  policyPath: string;
}) {
  const floorSet = new Set(catastrophicFloor);
  const extras = activeBlock.filter((c) => !floorSet.has(c));
  return (
    <div className={styles.panel}>
      <p className={styles.panelLabel}>active block</p>
      <ul className={styles.chipList} aria-label="active block categories">
        {catastrophicFloor.map((cat) => (
          <li key={cat} className={cn(styles.chip, styles.chipCatastrophic)}>
            <span className={styles.chipGlyph} aria-hidden="true">
              ◐
            </span>
            <span>{cat.replace(/_/g, " ")}</span>
            <span className={styles.chipTag}>floor</span>
          </li>
        ))}
        {extras.map((cat) => (
          <li key={cat} className={cn(styles.chip, styles.chipExtra)}>
            <span>{cat.replace(/_/g, " ")}</span>
          </li>
        ))}
      </ul>
      <p className={styles.panelFootnote}>
        {blockSource === "yaml" ? (
          <Fragment>
            source: <code className={styles.inlineCode}>{policyPath}</code>
          </Fragment>
        ) : (
          <Fragment>
            source: <span className={styles.muted}>default · no policy file</span>
          </Fragment>
        )}
      </p>
    </div>
  );
}

function DiffPreviewPanel({
  diff,
  totalRows,
}: {
  diff: PolicyResponse["diff_preview"];
  totalRows: number;
}) {
  const rows: Array<{
    key: string;
    label: string;
    value: number;
    isCurrent?: boolean;
  }> = [
    { key: "current", label: "current", value: diff.current_blocked, isCurrent: true },
    { key: "catastrophic", label: "catastrophic-only", value: diff.if_catastrophic_only },
    { key: "all", label: "block-all", value: diff.if_all_blocked },
    { key: "none", label: "none-blocked", value: diff.if_none_blocked },
  ];
  return (
    <div className={styles.panel}>
      <p className={styles.panelLabel}>posture preview</p>
      <table className={styles.diffTable} aria-label="diff preview across postures">
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className={cn(row.isCurrent && styles.diffCurrent)}>
              <th scope="row" className={styles.diffLabel}>
                {row.label}
                {row.isCurrent && <span className={styles.diffMarker}>·</span>}
              </th>
              <td className={styles.diffValue}>{row.value}</td>
              <td className={styles.diffUnit}>
                {row.value === 1 ? "column" : "columns"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className={styles.panelFootnote}>
        {totalRows === 0
          ? "no tagged columns in this source"
          : `out of ${totalRows} tagged ${totalRows === 1 ? "column" : "columns"} total`}
      </p>
    </div>
  );
}

/* ─────────── category rollup ─────────── */

function CategoryRollup({
  rows,
  catastrophicFloor,
}: {
  rows: readonly PolicyCategoryRollup[];
  catastrophicFloor: readonly PIICategory[];
}) {
  const floorSet = new Set(catastrophicFloor);
  // Hide the long zero-tail; the per-column table already covers
  // every tagged column. Keep zero-count floor rows so operators
  // see the safety net is wired even when the source happens to
  // have no credential/government_id/payment_card columns.
  const visible = rows.filter(
    (r) => r.column_count > 0 || floorSet.has(r.category),
  );
  if (visible.length === 0) {
    return null;
  }
  return (
    <div className={styles.rollupBlock}>
      <h3 className={styles.rollupTitle}>Category rollup</h3>
      <table className={styles.rollupTable} aria-label="pii category rollup">
        <thead>
          <tr>
            <th scope="col">category</th>
            <th scope="col" className={styles.rollupNumeric}>
              cols
            </th>
            <th scope="col" className={styles.rollupNumeric}>
              entities
            </th>
            <th scope="col">samples</th>
            <th scope="col">enforcement</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((row) => (
            <tr key={row.category}>
              <th scope="row" className={styles.rollupCategory}>
                {row.blocked_by_catastrophic_floor && (
                  <span className={styles.rollupFloorGlyph} aria-hidden="true">
                    ◐
                  </span>
                )}
                {row.category.replace(/_/g, " ")}
              </th>
              <td className={styles.rollupNumeric}>{row.column_count}</td>
              <td className={styles.rollupNumeric}>{row.entity_count}</td>
              <td className={styles.rollupSamples}>
                {row.sample_columns.length === 0 ? (
                  <span className={styles.muted}>—</span>
                ) : (
                  row.sample_columns.join(" · ")
                )}
              </td>
              <td>
                <EnforcementChip
                  blockedByActive={row.blocked_by_active_policy}
                  blockedByFloor={row.blocked_by_catastrophic_floor}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EnforcementChip({
  blockedByActive,
  blockedByFloor,
}: {
  blockedByActive: boolean;
  blockedByFloor: boolean;
}) {
  if (blockedByActive) {
    return <span className={cn(styles.miniChip, styles.miniChipBlocked)}>blocked</span>;
  }
  if (blockedByFloor) {
    return (
      <span className={cn(styles.miniChip, styles.miniChipDescribeBlocked)}>
        describe-only
      </span>
    );
  }
  return <span className={cn(styles.miniChip, styles.miniChipAllowed)}>allowed</span>;
}

/* ─────────── per-column table ─────────── */

const VERDICT_OPTIONS: readonly EffectiveEnforcement[] = [
  "allowed",
  "describe_blocked",
  "blocked",
];
const ORIGIN_OPTIONS: readonly PolicyOrigin[] = ["heuristic", "operator"];

function PerColumnTable({ rows }: { rows: readonly PolicyColumnEntry[] }) {
  const [verdictFilter, setVerdictFilter] = useState<"all" | EffectiveEnforcement>("all");
  const [originFilter, setOriginFilter] = useState<"all" | PolicyOrigin>("all");

  const filtered = useMemo(() => {
    return rows.filter((r) => {
      if (verdictFilter !== "all" && r.effective_enforcement !== verdictFilter) {
        return false;
      }
      if (originFilter !== "all" && r.origin !== originFilter) {
        return false;
      }
      return true;
    });
  }, [rows, verdictFilter, originFilter]);

  return (
    <div className={styles.columnsBlock}>
      <div className={styles.columnsHeader}>
        <h3 className={styles.columnsTitle}>Per-column verdict</h3>
        <div className={styles.filters}>
          <label className={styles.filterLabel}>
            verdict
            <select
              className={styles.filterSelect}
              value={verdictFilter}
              onChange={(e) =>
                setVerdictFilter(e.target.value as "all" | EffectiveEnforcement)
              }
              aria-label="filter by verdict"
            >
              <option value="all">all</option>
              {VERDICT_OPTIONS.map((v) => (
                <option key={v} value={v}>
                  {v.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.filterLabel}>
            origin
            <select
              className={styles.filterSelect}
              value={originFilter}
              onChange={(e) =>
                setOriginFilter(e.target.value as "all" | PolicyOrigin)
              }
              aria-label="filter by origin"
            >
              <option value="all">all</option>
              {ORIGIN_OPTIONS.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {filtered.length === 0 ? (
        <p className={styles.empty}>
          {rows.length === 0
            ? "no columns have been tagged on this source yet"
            : "no columns match the active filter"}
        </p>
      ) : (
        <div className={styles.tableWrap}>
          <table className={styles.columnsTable} aria-label="per-column verdict">
            <thead>
              <tr>
                <th scope="col">column</th>
                <th scope="col">sensitivity</th>
                <th scope="col">categories</th>
                <th scope="col">origin</th>
                <th scope="col">verdict</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <PerColumnRow key={row.qualified_column} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className={styles.footnote}>
        <span className={styles.footnoteMarker}>*</span> operator override · edit via{" "}
        <code className={styles.inlineCode}>schemabrain policy tag</code> or by editing{" "}
        <code className={styles.inlineCode}>pii_policy.yaml</code> and running{" "}
        <code className={styles.inlineCode}>schemabrain policy apply</code>
      </p>
    </div>
  );
}

function PerColumnRow({ row }: { row: PolicyColumnEntry }) {
  const isOperator = row.origin === "operator";
  return (
    <tr className={cn(isOperator && styles.operatorRow)}>
      <th scope="row" className={styles.colCell}>
        <span className={styles.colTable}>{row.qualified_table}</span>
        <span className={styles.colName}>
          {isOperator && (
            <span className={styles.operatorMarker} aria-label="operator override">
              *
            </span>
          )}
          {row.column_name}
        </span>
      </th>
      <td className={styles.sensitivityCell}>{row.sensitivity}</td>
      <td className={styles.categoriesCell}>
        {row.categories.length === 0 ? (
          <span className={styles.muted}>—</span>
        ) : (
          row.categories.map((c) => c.replace(/_/g, " ")).join(" · ")
        )}
      </td>
      <td>
        <OriginBadge origin={row.origin} />
      </td>
      <td>
        <VerdictBadge verdict={row.effective_enforcement} />
      </td>
    </tr>
  );
}

function OriginBadge({ origin }: { origin: PolicyOrigin }) {
  return (
    <span
      className={cn(
        styles.miniChip,
        origin === "operator" ? styles.miniChipOperator : styles.miniChipHeuristic,
      )}
    >
      {origin}
    </span>
  );
}

function VerdictBadge({ verdict }: { verdict: EffectiveEnforcement }) {
  if (verdict === "blocked") {
    return <span className={cn(styles.miniChip, styles.miniChipBlocked)}>blocked</span>;
  }
  if (verdict === "describe_blocked") {
    return (
      <span className={cn(styles.miniChip, styles.miniChipDescribeBlocked)}>
        describe-only
      </span>
    );
  }
  return <span className={cn(styles.miniChip, styles.miniChipAllowed)}>allowed</span>;
}

/* ─────────── error state ─────────── */

function PolicyError({ message }: { message: string }) {
  const isNoSource =
    message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <section className={styles.shell} aria-label="pii policy">
      <div className={styles.titleStrip}>
        <h2 className={styles.surfaceTitle}>Active Policy</h2>
        <p className={styles.titleMeta}>
          {isNoSource ? "status — no source connected" : "status — error"}
        </p>
      </div>
      <div className={styles.errorCard}>
        <p>
          {isNoSource
            ? "Policy view needs an indexed source. Run `schemabrain init` then `schemabrain index`."
            : message}
        </p>
      </div>
    </section>
  );
}
