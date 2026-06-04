"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Icon, type IconName, PiiChip, useToast } from "@/components/kit";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatRelativeTime } from "@/lib/relativeTime";
import { useSourceId } from "@/lib/useSourceId";
import type { DriftItem, DriftKind, DriftResponse } from "@/lib/types";
import styles from "./drift.module.css";

/**
 * Drift — the read-only context-health surface (/drift), matching the
 * design handoff (app/drift.jsx): a fresh/stale hero, a kind filter, and
 * risk-badged change cards. Honest adaptation: the only kinds are
 * `config` (policy file vs serve) and `enrichment` (prompt-version
 * staleness) — the store-only sidecar can't verify live schema/definition
 * drift, so those cards are omitted. The default is the fresh/empty
 * state, and every action copies a CLI command (ADR 0006) — the dashboard
 * never writes and never flips the hero client-side.
 */
const KIND_ICON: Record<DriftKind, IconName> = {
  config: "sliders-horizontal",
  enrichment: "sparkles",
};

const KIND_LABEL: Record<DriftKind, string> = {
  config: "Config",
  enrichment: "Enrichment",
};

const RISK_ICON_CLASS: Record<DriftItem["risk"], string> = {
  high: styles.riskHigh,
  med: styles.riskMed,
  low: styles.riskLow,
};

// Full-word accessible name for the compact "MED" badge — screen readers
// announce "Medium risk" while sighted users see the abbreviation.
const RISK_LABEL: Record<DriftItem["risk"], string> = {
  high: "High risk",
  med: "Medium risk",
  low: "Low risk",
};

type KindFilter = "all" | DriftKind;

const FILTERS: readonly { value: KindFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "config", label: "Config" },
  { value: "enrichment", label: "Enrichment" },
];

export function Drift({ sourceId: sourceIdProp }: { sourceId?: string }) {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const sourceId = sourceIdProp ?? resolvedSourceId ?? undefined;

  const driftQuery = useQuery({
    queryKey: ["drift", sourceId],
    queryFn: () => api.drift(sourceId),
    enabled: sourceIdProp !== undefined || sourceStatus !== "loading",
  });

  if (driftQuery.isPending) {
    return (
      <div className={styles.page}>
        <PageHead />
        <p className={styles.skeleton}>checking for drift…</p>
      </div>
    );
  }
  if (driftQuery.isError) {
    return <DriftError message={driftQuery.error.message} />;
  }

  return <DriftContent data={driftQuery.data} />;
}

function DriftContent({ data }: { data: DriftResponse }) {
  const { copyToClipboard } = useToast();
  const [filter, setFilter] = useState<KindFilter>("all");

  const { freshness, items } = data;
  // Single source of truth: derive fresh from items (the backend guarantees
  // freshness.fresh === items.length === 0) so the hero and the empty-state
  // branch can never disagree. Still data-driven — no client false-flip.
  const fresh = items.length === 0;
  const visible = items.filter((item) => filter === "all" || item.kind === filter);

  // The hero CTA copies the primary remedy for the current drift:
  // enrichment ("re-enrich") takes precedence over config ("restart"),
  // since the hero frames overall context staleness. Each card also
  // carries its own action, so config drift is still independently
  // actionable below.
  const primaryAction =
    items.find((item) => item.kind === "enrichment")?.action ??
    items.find((item) => item.kind === "config")?.action ??
    null;

  const lastEnriched =
    freshness.last_enriched === null ? "never" : formatRelativeTime(freshness.last_enriched);

  return (
    <div className={styles.page}>
      <PageHead />

      <section
        className={cn(styles.hero, fresh ? styles.fresh : styles.stale)}
        aria-label="Drift status"
      >
        <div className={styles.ring}>
          <Icon name={fresh ? "shield-check" : "radar"} size={28} />
        </div>
        <div className={styles.heroBody}>
          <h2>{fresh ? "Context is fresh" : "AI context may be stale"}</h2>
          <p>
            {fresh
              ? "No drift detected — your policy, classifications, and AI descriptions reflect the current source."
              : `${freshness.change_count} ${freshness.change_count === 1 ? "change" : "changes"} since the last check${
                  freshness.high_risk > 0 ? `, ${freshness.high_risk} high-risk` : ""
                }. Agents may be reasoning over an out-of-date picture until you resolve them.`}
          </p>
          <div className={styles.meta}>last enriched · {lastEnriched}</div>
        </div>
        <div className={styles.cta}>
          {fresh || primaryAction === null ? (
            <span className={styles.upToDate}>
              <span className={styles.upToDateIcon}>
                <Icon name="check" size={14} />
              </span>{" "}
              Up to date
            </span>
          ) : (
            <Button
              variant="primary"
              icon="rotate-cw"
              onClick={() =>
                copyToClipboard(primaryAction.command, stripCopyPrefix(primaryAction.label))
              }
            >
              {primaryAction.label}
            </Button>
          )}
        </div>
      </section>

      {items.length === 0 ? (
        <EmptyState />
      ) : (
        <>
          <div className={styles.toolbar}>
            <div className={styles.seg} role="group" aria-label="Filter drift by kind">
              {FILTERS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  className={cn(filter === option.value && styles.segActive)}
                  aria-pressed={filter === option.value}
                  onClick={() => setFilter(option.value)}
                >
                  {option.label}
                </button>
              ))}
            </div>
            <span className={styles.count}>
              {visible.length} {visible.length === 1 ? "change" : "changes"}
            </span>
          </div>

          <div className={styles.list} aria-label="Drift changes">
            {visible.length === 0 ? (
              <p className={styles.skeleton}>no {filter} drift</p>
            ) : (
              visible.map((item) => (
                <DriftCard
                  key={`${item.kind}:${item.title}`}
                  item={item}
                  onCopy={() =>
                    copyToClipboard(item.action.command, stripCopyPrefix(item.action.label))
                  }
                />
              ))
            )}
          </div>
        </>
      )}

      {/* Live region: text changes on filter so the result count is
          announced when the user switches All/Config/Enrichment (WCAG 4.1.3). */}
      <p className={styles.srOnly} role="status" aria-live="polite">
        {fresh
          ? "No drift detected"
          : filter === "all"
            ? `${visible.length} drift ${visible.length === 1 ? "change" : "changes"} detected`
            : `Showing ${visible.length} ${filter} ${visible.length === 1 ? "change" : "changes"}`}
      </p>
    </div>
  );
}

function DriftCard({ item, onCopy }: { item: DriftItem; onCopy: () => void }) {
  return (
    <div className={styles.card}>
      <div className={cn(styles.icon, RISK_ICON_CLASS[item.risk])}>
        <Icon name={KIND_ICON[item.kind]} size={17} />
      </div>
      <div>
        <div className={styles.top}>
          <span className={styles.title}>{item.title}</span>
          <span className={cn(styles.risk, styles[item.risk])}>
            <span className={styles.srOnly}>{RISK_LABEL[item.risk]}</span>
            <span aria-hidden="true">{item.risk}</span>
          </span>
          <PiiChip kind="neutral">{KIND_LABEL[item.kind]}</PiiChip>
          {item.when && <span className={styles.when}>{formatWhen(item.when)}</span>}
        </div>
        <div className={styles.detail}>{item.detail}</div>
      </div>
      <div className={styles.act}>
        <Button icon="copy" onClick={onCopy} aria-label={item.action.label}>
          {item.action.label}
        </Button>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className={styles.empty}>
      <span className={styles.emptyIcon}>
        <Icon name="shield-check" size={28} />
      </span>
      <h3>No drift detected</h3>
      <p>
        Your policy, classifications, and AI descriptions all reflect the current source.
        SchemaBrain re-checks every time this page loads.
      </p>
    </div>
  );
}

function PageHead() {
  return (
    <header className={styles.pageHead}>
      <h1>Drift</h1>
      <p>
        SchemaBrain watches the policy you enforce and the AI context it built. When either moves
        out of sync with the running firewall, agents can reason over an out-of-date picture — here
        is what changed and how to resolve it.
      </p>
    </header>
  );
}

function DriftError({ message }: { message: string }) {
  const isNoSource = message.includes("409") || message.toLowerCase().includes("no source");
  return (
    <div className={styles.page}>
      <PageHead />
      <div className={styles.errorCard}>
        {isNoSource
          ? "Drift needs an indexed source. Run `schemabrain init` then `schemabrain index`."
          : message}
      </div>
    </div>
  );
}

/** "Copy restart command" → "restart command" for the clipboard toast. */
function stripCopyPrefix(label: string): string {
  return label.replace(/^Copy\s+/, "");
}

/** Config drift `when` is ISO8601 — render it as relative time to match the
 * hero (and the handoff), falling back to the raw string if it won't parse. */
function formatWhen(iso: string): string {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? iso : formatRelativeTime(ms / 1000);
}
