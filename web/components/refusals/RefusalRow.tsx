"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { isCatastrophic, type RefusalEntry } from "@/lib/types";
import { prettyCategory } from "@/lib/policy";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  buildRecoveryHint,
  bucketChipKind,
  bucketIcon,
  bucketLabel,
  categoryChipKind,
  isFloorRefusal,
  refusalBucket,
  type RefusalBucket,
} from "@/lib/refusals";
import { Eyebrow, Icon, PiiChip, useToast } from "@/components/kit";
import styles from "./ledger.module.css";

interface RefusalRowProps {
  entry: RefusalEntry;
  expanded: boolean;
  onToggle: () => void;
  /** Injected "now" so the relative time renders deterministically. */
  nowMs: number;
}

const RAIL_CLASS: Record<RefusalBucket, string> = {
  sensitive: styles.railSensitive,
  unsafe: styles.railUnsafe,
  integrity: styles.railIntegrity,
};

/** HH:MM clock for the time gutter; "—" for an unparseable instant (never a
 * fabricated time). */
function formatClock(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function RefusalRow({ entry, expanded, onToggle, nowMs }: RefusalRowProps) {
  const bucket = refusalBucket(entry.refusal_reason);
  const floor = isFloorRefusal(entry);
  const epochSeconds = Math.floor(Date.parse(entry.occurred_at) / 1000);
  const ago = Number.isNaN(epochSeconds) ? "—" : formatRelativeTime(epochSeconds, nowMs);
  const regionId = `refusal-detail-${entry.id}`;

  const rowClass = [
    styles.row,
    floor ? styles.rowFloor : RAIL_CLASS[bucket],
    expanded ? styles.rowOpen : "",
  ]
    .filter(Boolean)
    .join(" ");

  // A sentence accessible name — the concatenated cell text otherwise reads as
  // ambiguous tokens ("5m ago 14:32 Sensitive-data floor query_rows() …"). The
  // visual cells stay; this just gives AT a coherent summary + the toggle verb.
  const rowLabel =
    `${bucketLabel(bucket)} refusal${floor ? ", catastrophic floor" : ""} — ` +
    `${entry.tool_name}, ${ago}, held. ${expanded ? "Hide" : "Show"} envelope.`;

  return (
    <li className={styles.rowWrap}>
      <button
        type="button"
        className={rowClass}
        onClick={onToggle}
        aria-expanded={expanded}
        // Only reference the region while it is mounted (it renders only when
        // expanded) — a dangling aria-controls is an invalid ARIA reference.
        aria-controls={expanded ? regionId : undefined}
        aria-label={rowLabel}
      >
        {/* time */}
        <span className={styles.timeCell}>
          <time className={styles.timeAgo} dateTime={entry.occurred_at}>
            {ago}
          </time>
          <span className={styles.timeClock}>{formatClock(entry.occurred_at)}</span>
        </span>

        {/* reason bucket badge */}
        <span className={styles.reasonCell}>
          <PiiChip kind={bucketChipKind(bucket, floor)} dot>
            <Icon name={bucketIcon(bucket)} size={12} />
            <span>{bucketLabel(bucket)}</span>
          </PiiChip>
          {floor && <span className={styles.floorTag}>floor</span>}
        </span>

        {/* tool · caller */}
        <span className={styles.toolCell}>
          <span className={styles.toolName}>{entry.tool_name}()</span>
          <PiiChip kind="neutral" className={styles.callerChip}>
            <Icon name="database" size={11} />
            <span className={styles.callerName}>{entry.caller_id ?? "agent"}</span>
          </PiiChip>
        </span>

        {/* categories */}
        <span className={styles.catCell}>
          {entry.pii_categories.length === 0 ? (
            <span className={styles.catEmpty}>—</span>
          ) : (
            entry.pii_categories.map((cat) => (
              <PiiChip key={cat} kind={categoryChipKind(cat)} className={styles.catChip}>
                {isCatastrophic(cat) && <Icon name="lock" size={11} />}
                <span>{prettyCategory(cat)}</span>
              </PiiChip>
            ))
          )}
        </span>

        {/* status — the always-true protective affirmation */}
        <span className={styles.statusCell}>
          <PiiChip kind="green" className={styles.heldChip}>
            <Icon name="shield-check" size={12} />
            <span>held</span>
          </PiiChip>
          <Icon
            name="chevron-down"
            size={14}
            className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`}
          />
        </span>
      </button>

      {expanded && <RefusalDetail entry={entry} regionId={regionId} />}
    </li>
  );
}

/* ─────────── expanded envelope detail (fires the detail query on mount) ─────────── */

function RefusalDetail({ entry, regionId }: { entry: RefusalEntry; regionId: string }) {
  const { copyToClipboard } = useToast();
  const detailQuery = useQuery({
    queryKey: ["audit-refusal-detail", entry.id],
    queryFn: () => api.auditRefusalDetail(entry.id),
  });

  const recoveryHint = buildRecoveryHint(entry.pii_categories);
  // The sidecar reconstructs the full envelope; until it resolves (or if the
  // fetch fails) we show only the fields the audit row itself carries, labelled
  // honestly so it never reads as the live charter envelope.
  const fetched = detailQuery.data?.envelope;
  const envelope = fetched ?? {
    status: "refused",
    error: {
      kind: entry.refusal_reason ?? "pii_blocked",
      pii_categories: entry.pii_categories,
    },
  };
  const envelopeJson = JSON.stringify(envelope, null, 2);

  // The version label tracks the envelope actually shown — never a hardcoded
  // constant that could drift from what the sidecar emits.
  const codeLabel = detailQuery.isPending
    ? "loading…"
    : detailQuery.isError
      ? "detail fetch failed"
      : fetched?.charter_version
        ? `charter v${fetched.charter_version}`
        : "reconstructed";

  return (
    <div
      id={regionId}
      role="region"
      aria-label={`refusal ${entry.id} envelope`}
      aria-busy={detailQuery.isPending}
      className={styles.expandRegion}
    >
      <div className={styles.expandInner}>
        {/* recovery hint — the same guidance the agent received */}
        <section className={styles.detailSection}>
          <Eyebrow>Recovery hint returned to the agent</Eyebrow>
          <p className={styles.hintLine}>{recoveryHint}</p>
        </section>

        {/* reconstructed envelope */}
        <section className={styles.detailSection}>
          <div className={styles.detailHead}>
            <Eyebrow>Reconstructed envelope</Eyebrow>
            <button
              type="button"
              className={styles.copyHash}
              onClick={() => copyToClipboard(envelopeJson, "envelope copied")}
            >
              <Icon name="copy" size={13} /> copy
            </button>
          </div>
          <p className={styles.detailCaption}>
            {detailQuery.isError
              ? "Couldn’t load the full envelope from the sidecar — showing the fields recorded on the audit row."
              : "Reconstructed from the audit row’s structured columns — the original request was never stored."}
          </p>
          <div className={styles.codePanel}>
            <div className={styles.codeHeader}>
              <span className={styles.codeTitle}>tool response (refused)</span>
              <span className={styles.codeTitle}>{codeLabel}</span>
            </div>
            <pre className={styles.codeArea}>{envelopeJson}</pre>
          </div>
        </section>

        {/* audit row — the tamper-evident chain link */}
        <section className={styles.detailSection}>
          <Eyebrow>Audit row · tamper-evident chain</Eyebrow>
          <div className={styles.specList}>
            <span className={styles.specLabel}>source</span>
            <span className={styles.specValue}>{entry.source_connection_id}</span>

            <span className={styles.specLabel}>fingerprint</span>
            <button
              type="button"
              className={styles.copyHash}
              onClick={() => copyToClipboard(entry.fingerprint_hex, "fingerprint copied")}
              aria-label={`Copy fingerprint ${entry.fingerprint_hex}`}
            >
              {entry.fingerprint_hex}
            </button>

            <span className={styles.specLabel}>chain hash</span>
            <button
              type="button"
              className={styles.copyHash}
              onClick={() => copyToClipboard(entry.chain_hash_hex, "chain hash copied")}
              aria-label={`Copy chain hash ${entry.chain_hash_hex}`}
            >
              {entry.chain_hash_hex}
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}
