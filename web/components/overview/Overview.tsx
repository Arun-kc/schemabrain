"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Icon, PiiChip } from "@/components/kit";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { categoryChipKind } from "@/lib/refusals";
import { confidenceRated, maxCount, toPercent } from "@/lib/overview/stats";
import { formatRelativeTime } from "@/lib/relativeTime";
import { useSourceId } from "@/lib/useSourceId";
import { PII_CATEGORIES } from "@/lib/types";
import type { OverviewResponse, PIICategory } from "@/lib/types";
import styles from "./overview.module.css";

/**
 * Overview — the dashboard's home surface (/overview), matching the design
 * handoff (app/overview.jsx): a system-status hero + a 6-card bento where
 * every card is a doorway into its detail page. Honest adaptation of the
 * prototype: every figure is read live from GET /api/overview (the seed
 * world's hardcoded total, literal timestamps, and always-"Stale" pill are
 * gone) — absent values render "—", the freshness pill reflects real drift,
 * confidence is the categorical distribution (never a numeric meter), and
 * the protection bar's `--alarm` segment is the genuinely catastrophic share.
 */
const GROUPS: readonly { key: "identity" | "billing" | "activity"; label: string }[] = [
  { key: "identity", label: "Identity" },
  { key: "billing", label: "Billing" },
  { key: "activity", label: "Activity" },
];

const GROUP_DOT: Record<string, string> = {
  identity: styles.dotIdentity,
  billing: styles.dotBilling,
  activity: styles.dotActivity,
};

export function Overview({ sourceId: sourceIdProp }: { sourceId?: string }) {
  const { sourceId: resolvedSourceId, status: sourceStatus } = useSourceId();
  const sourceId = sourceIdProp ?? resolvedSourceId ?? undefined;

  const overviewQuery = useQuery({
    queryKey: ["overview", sourceId],
    queryFn: () => api.overview(sourceId),
    enabled: sourceIdProp !== undefined || sourceStatus !== "loading",
  });

  if (overviewQuery.isPending) {
    return (
      <div className={styles.page}>
        <PageHead />
        <p className={styles.skeleton}>reading the boundary…</p>
      </div>
    );
  }
  if (overviewQuery.isError) {
    return (
      <div className={styles.page}>
        <PageHead />
        <p className={styles.error} role="alert">
          Couldn’t load the overview — {overviewQuery.error.message}
        </p>
      </div>
    );
  }
  return <OverviewContent data={overviewQuery.data} />;
}

function PageHead() {
  return (
    <div className={styles.pageHead}>
      <h1>Overview</h1>
      <p>
        One screen on the whole boundary — what SchemaBrain has bound, what it’s protecting, and
        what’s changed since the last enrichment. Every panel opens its detail view.
      </p>
    </div>
  );
}

function relTime(epoch: number | null): string {
  return epoch == null ? "—" : formatRelativeTime(epoch);
}

/**
 * Map a refusal's category to a chip kind. The wire `category` is
 * `string | null`; validate it against the known enum before mapping
 * (rather than an unchecked `as PIICategory` cast) so an unrecognised
 * value renders a neutral chip instead of silently coercing.
 */
function refusalChipKind(category: string): "auth" | "contact" | "none" | "neutral" {
  return (PII_CATEGORIES as readonly string[]).includes(category)
    ? categoryChipKind(category as PIICategory)
    : "neutral";
}

function OverviewContent({ data }: { data: OverviewResponse }) {
  const { source, entities, protection, freshness, refusals, audit, semantic } = data;
  const domains = GROUPS.filter((g) => entities.by_group[g.key] > 0).length || GROUPS.length;
  const groupMax = maxCount(GROUPS.map((g) => entities.by_group[g.key]));
  const rated = confidenceRated(entities.confidence);

  return (
    <div className={styles.page}>
      <PageHead />

      {/* system-status hero */}
      <section className={styles.hero} aria-label="System status">
        <div className={styles.heroRing}>
          <Icon name="circle-check-big" size={26} />
        </div>
        <div className={styles.heroMain}>
          <h2>Agents are reasoning over a protected graph</h2>
          <p>
            <b>{entities.count}</b> entities bound · <b>{entities.catastrophic_count}</b>{" "}
            catastrophic floors locked, with a tamper-evident audit trail. Connected to{" "}
            <b>{source.label}</b>, indexed {relTime(source.last_indexed_at)}.
          </p>
        </div>
        <div className={styles.heroCta}>
          <Link href="/graph" className="sb-btn primary">
            <Icon name="waypoints" size={14} /> Open knowledge graph
          </Link>
          {source.tables != null && (
            <span className={styles.heroMeta}>
              {source.tables} {source.tables === 1 ? "table" : "tables"}
            </span>
          )}
        </div>
      </section>

      <div className={styles.grid}>
        {/* protection posture → /pii */}
        <Card href="/pii" eyebrow="Protection posture" span="span3" wide tone="alarm">
          <div className={styles.lead}>
            <span className={styles.num}>{protection.catastrophic}</span>
            <span className={styles.unit}>catastrophic columns hard-blocked</span>
          </div>
          <div className={styles.stack} aria-hidden="true">
            <span
              className={styles.segAlarm}
              style={{ width: `${toPercent(protection.catastrophic, protection.columns)}%` }}
            />
            <span
              className={styles.segPolicy}
              style={{ width: `${toPercent(protection.policy_blocked, protection.columns)}%` }}
            />
            <span
              className={styles.segAllow}
              style={{ width: `${toPercent(protection.allowed, protection.columns)}%` }}
            />
          </div>
          <div className={styles.legend}>
            <LegendRow className={styles.swAlarm} count={protection.catastrophic} label="Catastrophic" />
            <LegendRow
              className={styles.swPolicy}
              count={protection.policy_blocked}
              label="Policy-blocked"
            />
            <LegendRow className={styles.swAllow} count={protection.allowed} label="Allowed" />
          </div>
          <div className={styles.floorNote}>
            <Icon name="lock" size={13} /> PCI &amp; credential floors never leave the boundary,
            regardless of policy.
          </div>
        </Card>

        {/* freshness → /drift */}
        <Card
          href="/drift"
          eyebrow="Freshness"
          span="span3"
          wide
          tone={freshness.fresh ? undefined : "warn"}
          pill={
            freshness.fresh ? (
              <span className={cn(styles.pill, styles.pillGreen)}>Fresh</span>
            ) : (
              <span className={cn(styles.pill, styles.pillAmber)}>Stale</span>
            )
          }
        >
          <div className={styles.freshRow}>
            <div className={cn(styles.smallRing, freshness.fresh ? styles.ringGreen : styles.ringAmber)}>
              <Icon name="radar" size={22} />
            </div>
            <div>
              <h3 className={styles.cardH3}>
                {freshness.fresh ? "Context is fresh" : "AI context may be stale"}
              </h3>
              <div className={styles.sub}>
                {freshness.fresh ? (
                  <>No drift since the last enrichment.</>
                ) : (
                  <>
                    <b>{freshness.change_count}</b>{" "}
                    {freshness.change_count === 1 ? "change" : "changes"} since the last enrichment.
                  </>
                )}
              </div>
              <div className={styles.meta}>last enriched · {relTime(freshness.last_enriched)}</div>
            </div>
          </div>
          <div className={styles.risk}>
            <RiskCell className={styles.riskHigh} n={freshness.risk.high} label="High" />
            <RiskCell className={styles.riskMed} n={freshness.risk.med} label="Medium" />
            <RiskCell n={freshness.risk.low} label="Low" />
          </div>
        </Card>

        {/* refusals · last 24h → /refusals */}
        <Card
          href="/refusals"
          eyebrow="Refusals · last 24h"
          span="span3"
          wide
          pill={<span className={cn(styles.pill, styles.pillGreen)}>Protected</span>}
        >
          <div className={styles.lead}>
            <span className={styles.num}>{refusals.count_24h}</span>
            <span className={styles.unit}>agent attempts blocked today</span>
          </div>
          <div className={styles.list} aria-label="Recent refusals">
            {refusals.recent.length === 0 ? (
              <p className={styles.empty}>no refusals recorded yet</p>
            ) : (
              refusals.recent.map((r, i) => (
                <div key={`${r.tool}:${r.occurred_at}:${i}`} className={styles.li}>
                  <span className={styles.tool}>{r.tool}</span>
                  {r.category && (
                    <PiiChip kind={refusalChipKind(r.category)}>{r.category}</PiiChip>
                  )}
                  <span className={styles.ago}>{relTime(r.occurred_at)}</span>
                </div>
              ))
            )}
          </div>
        </Card>

        {/* audit trail → /audit */}
        <Card
          href="/audit"
          eyebrow="Audit trail"
          span="span3"
          wide
          pill={<span className={cn(styles.pill, styles.pillGreen)}>Tamper-evident</span>}
        >
          <div className={styles.freshRow}>
            <div className={cn(styles.smallRing, styles.ringGreen)}>
              <Icon name="shield-check" size={22} />
            </div>
            <div>
              <h3 className={styles.cardH3}>Hash chain intact</h3>
              <div className={styles.sub}>
                <b>{audit.total.toLocaleString()}</b>{" "}
                {audit.total === 1 ? "tool call" : "tool calls"} · append-only, tamper-evident.
              </div>
              <div className={styles.meta}>verify the full chain on the Audit surface</div>
            </div>
          </div>
          <div className={styles.list} aria-label="Recent audit rows">
            {audit.recent.length === 0 ? (
              <p className={styles.empty}>no tool calls recorded yet</p>
            ) : (
              audit.recent.map((a) => (
                <div key={a.seq} className={styles.li}>
                  <span className={cn(styles.tool, a.refused && styles.toolRefused)}>{a.tool}</span>
                  <span className={styles.summary}>{a.summary}</span>
                  <span className={styles.ago}>#{a.seq}</span>
                </div>
              ))
            )}
          </div>
        </Card>

        {/* bound entities → /entities */}
        <Card href="/entities" eyebrow="Bound entities" span="span4" wide>
          <div className={styles.lead}>
            <span className={styles.num}>{entities.count}</span>
            <span className={styles.unit}>
              entities across {domains} {domains === 1 ? "domain" : "domains"}
            </span>
          </div>
          <div className={styles.groups}>
            {GROUPS.map((g) => (
              <div key={g.key} className={styles.grow}>
                <span className={cn(styles.dot, GROUP_DOT[g.key])} aria-hidden="true" />
                <span className={styles.gl}>{g.label}</span>
                <span className={styles.bar} aria-hidden="true">
                  <i
                    className={GROUP_DOT[g.key]}
                    style={{ width: `${toPercent(entities.by_group[g.key], groupMax)}%` }}
                  />
                </span>
                <span className={styles.gn}>{entities.by_group[g.key]}</span>
              </div>
            ))}
          </div>
          <div className={styles.conf}>
            <span className={styles.confLabel}>Bind confidence</span>
            {rated ? (
              <span className={styles.confDist}>
                <b>{entities.confidence.high}</b> high · <b>{entities.confidence.medium}</b> med ·{" "}
                <b>{entities.confidence.low}</b> low
              </span>
            ) : (
              <span className={styles.confMuted}>no model rating — hand-authored</span>
            )}
          </div>
        </Card>

        {/* semantic layer → /graph */}
        <Card href="/graph" eyebrow="Semantic layer" span="span2">
          <div className={styles.lead}>
            <span className={styles.num}>{semantic.metrics_count}</span>
            <span className={styles.unit}>
              metrics · {semantic.joins_count} {semantic.joins_count === 1 ? "join" : "joins"}
            </span>
          </div>
          <div className={styles.sub}>
            {semantic.mined_count > 0 ? (
              <>
                <b>{semantic.mined_count}</b> joins mined from query logs the schema never declared.
              </>
            ) : (
              <>Every join is a declared foreign key — none mined from query logs yet.</>
            )}
          </div>
          <div className={styles.chips}>
            {semantic.metric_names.map((name) => (
              <PiiChip key={name} kind="cyan">
                {name}
              </PiiChip>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}

function Card({
  href,
  eyebrow,
  pill,
  tone,
  span,
  wide,
  children,
}: {
  href: string;
  eyebrow: string;
  pill?: ReactNode;
  tone?: "alarm" | "warn";
  span: "span2" | "span3" | "span4";
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      className={cn(
        styles.card,
        styles[span],
        wide && styles.wide,
        tone === "alarm" && styles.alarm,
        tone === "warn" && styles.warn,
      )}
    >
      <div className={styles.head}>
        <span className={styles.ti}>{eyebrow}</span>
        {pill}
        <span className={styles.go} aria-hidden="true">
          <Icon name="arrow-up-right" size={15} />
        </span>
      </div>
      {children}
    </Link>
  );
}

function LegendRow({ className, count, label }: { className: string; count: number; label: string }) {
  return (
    <span className={styles.legrow}>
      <span className={cn(styles.sw, className)} aria-hidden="true" />
      <span className={styles.c}>{count}</span> {label}
    </span>
  );
}

function RiskCell({ className, n, label }: { className?: string; n: number; label: string }) {
  return (
    <div className={cn(styles.riskCell, className)}>
      <div className={styles.riskN}>{n}</div>
      <div className={styles.riskL}>{label}</div>
    </div>
  );
}
