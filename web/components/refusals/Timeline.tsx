"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { RefusalEntry } from "@/lib/types";
import {
  bucketCounts,
  bucketLabel,
  BUCKET_ORDER,
  filterByBucket,
  type RefusalBucket,
  type RefusalSummary,
  summarizeRefusals,
} from "@/lib/refusals";
import { Icon } from "@/components/kit";
import { RefusalRow } from "./RefusalRow";
import styles from "./ledger.module.css";

/**
 * THE LEDGER — the Refusals protective timeline (/refusals), mounted inside the
 * app shell (the shell owns the chrome). The instrument-panel sibling of the
 * /pii heatmap: each blocked attempt is one banded row down a shared grid,
 * newest-first, with an alarm left-rail on the catastrophic floor. Read-only;
 * a row expands in place to reveal the reconstructed envelope (ADR 0001 — the
 * original request was never stored). Refusals are global (not source-scoped),
 * so this surface does not resolve a source id.
 */
export function Timeline() {
  // "now" pinned per mount so every relative time + window is stable.
  const [nowMs] = useState(() => Date.now());
  const [bucket, setBucket] = useState<RefusalBucket | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const refusalsQuery = useQuery({
    queryKey: ["audit-refusals"],
    queryFn: () => api.auditRefusals(),
  });

  if (refusalsQuery.isPending) {
    return (
      <div className={styles.shell}>
        <Header />
        <p className={styles.skeleton} role="status">
          reading the ledger…
        </p>
      </div>
    );
  }

  if (refusalsQuery.isError) {
    return (
      <div className={styles.shell}>
        <Header />
        <ErrorCard message={refusalsQuery.error.message} />
      </div>
    );
  }

  const { items, total } = refusalsQuery.data;
  const summary = summarizeRefusals(items, nowMs);
  const counts = bucketCounts(items);
  const visible = filterByBucket(items, bucket);

  return (
    <div className={styles.shell}>
      <Header />

      {total === 0 ? (
        <EmptyState />
      ) : (
        <>
          <StatStrip summary={summary} total={total} inView={items.length} />
          <FilterBar
            counts={counts}
            allCount={items.length}
            active={bucket}
            onSelect={setBucket}
            visibleCount={visible.length}
          />
          <div className={styles.ledgerPanel}>
            <div className={styles.ledgerHead} aria-hidden="true">
              <span>time</span>
              <span>reason</span>
              <span>tool · caller</span>
              <span>categories</span>
              <span className={styles.headStatus}>status</span>
            </div>
            <ul className={styles.list} aria-label="refusals ledger">
              {visible.map((entry) => (
                <RefusalRow
                  key={entry.id}
                  entry={entry}
                  expanded={expandedId === entry.id}
                  onToggle={() =>
                    setExpandedId((prev) => (prev === entry.id ? null : entry.id))
                  }
                  nowMs={nowMs}
                />
              ))}
            </ul>
            <Legend />
          </div>
        </>
      )}
    </div>
  );
}

/* ─────────── header (title strip + protective lede) ─────────── */

function Header() {
  return (
    <>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>Refusals</h1>
        <p className={styles.titleMeta}>held by definition · tamper-evident</p>
      </div>
      <p className={styles.lede}>
        Each row is a request SchemaBrain held at the boundary so your data never left the
        database. This is the system doing its job — not an error log.
      </p>
    </>
  );
}

/* ─────────── stat strip ─────────── */

function StatStrip({
  summary,
  total,
  inView,
}: {
  summary: RefusalSummary;
  total: number;
  inView: number;
}) {
  // The list is windowed (server default page) once total exceeds it. The
  // time-window + floor counts are then computed over the loaded rows only, so
  // we disclose that rather than presenting them as all-time absolutes —
  // silently undercounting the floor tally would be the unsafe direction.
  const windowed = inView < total;
  return (
    <>
      <div className={styles.summaryStrip} role="group" aria-label="refusal summary">
        <Stat n={summary.lastHour} label="held · last hour" tone={summary.lastHour > 0 ? "cyan" : "ink"} />
        <Stat n={summary.today} label="held · today" tone="ink" />
        <Stat
          n={summary.floorHits}
          label="floor categories held"
          tone={summary.floorHits > 0 ? "alarm" : "ink"}
        />
        <Stat
          n={total}
          label="refusals"
          tone="ink"
          sub={windowed ? `${inView} in view` : undefined}
        />
      </div>
      {windowed && (
        <p className={styles.windowNote}>
          Last-hour, today and floor counts cover the {inView} most recent refusals shown.
        </p>
      )}
    </>
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

/* ─────────── bucket filter ─────────── */

function FilterBar({
  counts,
  allCount,
  active,
  onSelect,
  visibleCount,
}: {
  counts: Record<RefusalBucket, number>;
  allCount: number;
  active: RefusalBucket | null;
  onSelect: (bucket: RefusalBucket | null) => void;
  visibleCount: number;
}) {
  const emptyBuckets = BUCKET_ORDER.filter((b) => counts[b] === 0);

  return (
    <>
      <div className={styles.controlBar}>
        <div className={styles.bucketSeg} role="group" aria-label="filter by protection">
          <SegButton label="All" count={allCount} on={active === null} onClick={() => onSelect(null)} />
          {BUCKET_ORDER.map((b) => (
            <SegButton
              key={b}
              label={bucketLabel(b)}
              count={counts[b]}
              on={active === b}
              disabled={counts[b] === 0}
              onClick={() => onSelect(b)}
            />
          ))}
        </div>
        <span
          className={styles.resultCount}
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {visibleCount} shown
        </span>
      </div>
      {emptyBuckets.length > 0 && (
        <p className={styles.filterHelper}>
          {emptyBuckets.map(bucketLabel).join(" · ")}{" "}
          {emptyBuckets.length === 1 ? "protection hasn’t" : "protections haven’t"} needed to fire yet.
        </p>
      )}
    </>
  );
}

function SegButton({
  label,
  count,
  on,
  disabled,
  onClick,
}: {
  label: string;
  count: number;
  on: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`${styles.segBtn} ${on ? styles.segOn : ""}`}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={on}
    >
      {label} <span className={styles.segCount}>{count}</span>
    </button>
  );
}

/* ─────────── legend ─────────── */

function Legend() {
  return (
    <div className={styles.legend}>
      <span className={styles.legendKey}>
        <span className={`${styles.legendSwatch} ${styles.swatchSensitive}`} /> sensitive-data
      </span>
      <span className={styles.legendKey}>
        <span className={`${styles.legendSwatch} ${styles.swatchUnsafe}`} /> unsafe-query
      </span>
      <span className={styles.legendKey}>
        <span className={`${styles.legendSwatch} ${styles.swatchIntegrity}`} /> integrity
      </span>
      <span className={styles.legendKey}>
        <span className={`${styles.legendSwatch} ${styles.swatchFloor}`} /> catastrophic floor
      </span>
      <span className={styles.legendNote}>
        request text is never stored — rows show only structured columns
      </span>
    </div>
  );
}

/* ─────────── empty / error states ─────────── */

function EmptyState() {
  return (
    <div className={styles.emptyState}>
      <Icon name="shield-check" size={40} className={styles.emptyIcon} label="nothing refused" />
      <h2 className={styles.emptyTitle}>Nothing needed holding.</h2>
      <p className={styles.emptyText}>
        No request has crossed a protected boundary yet. When an agent asks for sensitive
        data, the refusal — and the reason it was held — will appear here.
      </p>
    </div>
  );
}

function ErrorCard({ message }: { message: string }) {
  const isNoSource = message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <div className={styles.errorCard}>
      <span className={styles.errorIcon} aria-hidden="true">
        <Icon name={isNoSource ? "database" : "x"} size={22} />
      </span>
      <div className={styles.errorBody}>
        {isNoSource ? (
          <>
            <h2>Point SchemaBrain at a database.</h2>
            <p>The ledger fills in once a source is indexed and an agent makes a request.</p>
            <code>$ schemabrain init</code>
            <code>$ schemabrain index --target postgres://your-replica</code>
          </>
        ) : (
          <>
            <h2>Couldn’t load the ledger.</h2>
            <p>{message}</p>
            <p>
              The sidecar is running, but the refusals query failed. Check the sidecar logs
              in your terminal for the underlying cause.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
