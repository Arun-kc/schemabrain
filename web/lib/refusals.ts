// Pure refusals-feed logic — the 3-bucket reason grouping, the floor-aware
// chip mapping, the protective stat-strip tally, and the category-derived
// recovery hint. Framework-free (no React) so it is unit-testable in
// isolation. Sibling to `lib/piiMatrix.ts`.
//
// HONESTY CONTRACT: the timeline renders ONLY the structured columns the
// audit row actually persisted. The original request text / SQL / row content
// is NEVER stored (privacy-by-construction, ADR 0001), so there is no quote to
// show. A policy refusal is deterministic (the category is in the blocked
// set), so there is no honest per-event confidence to show either. The 3
// buckets below are a CLIENT projection over the 6 closed `refusal_reason`
// values; the recovery hint is DETERMINISTICALLY derived from the row's
// `pii_categories` (it is guidance, not a fabricated narrative). This module
// never invents a count, a category, or a sentence the data can't back.

import { isCatastrophic, type PIICategory, type RefusalReason } from "@/lib/types";
import type { AuditRow, RefusalEntry } from "@/lib/types";

/** The three protective buckets the six refusal reasons collapse into. */
export type RefusalBucket = "sensitive" | "unsafe" | "integrity";

/** Fixed render order — Sensitive-data leads (it is the only bucket the engine
 * emits today and the one carrying the catastrophic floor). */
export const BUCKET_ORDER: readonly RefusalBucket[] = ["sensitive", "unsafe", "integrity"];

/**
 * Map a refusal reason to its protective bucket (the locked 3-bucket grouping):
 *   sensitive  — pii_blocked, allowlist_violation
 *   unsafe     — fragment_unsafe, cost_cap_exceeded
 *   integrity  — ambiguous_resolution, schema_drift
 * A refused row always carries a reason in practice; a `null` reason (older or
 * loose rows) defaults to `sensitive` — the only bucket the engine emits today
 * is `pii_blocked`, and under-grouping toward the protective default is the
 * safe direction.
 */
export function refusalBucket(reason: RefusalReason | null): RefusalBucket {
  switch (reason) {
    case "fragment_unsafe":
    case "cost_cap_exceeded":
      return "unsafe";
    case "ambiguous_resolution":
    case "schema_drift":
      return "integrity";
    case "pii_blocked":
    case "allowlist_violation":
    case null:
      return "sensitive";
    default:
      // Exhaustiveness sentinel — if a 7th RefusalReason is added without a
      // case here, this line stops compiling. Falls back to the protective
      // default at runtime so a new reason is never silently dropped.
      reason satisfies never;
      return "sensitive";
  }
}

const BUCKET_LABEL: Record<RefusalBucket, string> = {
  sensitive: "Sensitive-data",
  unsafe: "Unsafe-query",
  integrity: "Integrity",
};

/** Human label for a bucket badge / filter segment. */
export function bucketLabel(bucket: RefusalBucket): string {
  return BUCKET_LABEL[bucket];
}

const BUCKET_ICON: Record<RefusalBucket, "shield-check" | "ban" | "scale"> = {
  sensitive: "shield-check",
  unsafe: "ban",
  integrity: "scale",
};

/** Reason icon for a bucket — a protective verb (guarded / refused /
 * disambiguated), carried on the badge so the bucket survives grayscale and
 * doesn't rely on color alone (WCAG 1.4.1). */
export function bucketIcon(bucket: RefusalBucket): "shield-check" | "ban" | "scale" {
  return BUCKET_ICON[bucket];
}

const BUCKET_BLURB: Record<RefusalBucket, string> = {
  sensitive: "personal, credential, or payment data the agent shouldn't read",
  unsafe: "queries whose cost or shape falls outside the safe envelope",
  integrity: "ambiguous or drifted references SchemaBrain couldn't resolve safely",
};

/** One-line plain-language description of what a bucket protects — used in the
 * legend / empty-bucket copy so the breadth of protection reads honestly
 * without implying volume that isn't there. */
export function bucketBlurb(bucket: RefusalBucket): string {
  return BUCKET_BLURB[bucket];
}

/**
 * A refusal is "floor" when it touched any catastrophic category
 * (credential / payment_card / government_id) — the only hard guarantee
 * SchemaBrain asserts: those are always refused, regardless of policy. Floor
 * refusals carry extra alarm weight inside the Sensitive-data bucket.
 */
export function isFloorRefusal(entry: Pick<AuditRow, "pii_categories">): boolean {
  return entry.pii_categories.some(isCatastrophic);
}

/**
 * Chip kind for one PII category. This is the fix the matrix's
 * `piiCategoryToChipKind` cannot provide: that helper matches the chip-kind
 * strings (`"auth"`/`"payment"`/`"contact"`), NOT the data-model category
 * names, so it silently renders `credential` / `payment_card` /
 * `government_id` muted. This maps the real category enum: catastrophic →
 * alarm `auth`, `contact` → amber `contact`, everything else → muted `none`.
 */
export function categoryChipKind(category: PIICategory): "auth" | "contact" | "none" {
  if (isCatastrophic(category)) return "auth";
  if (category === "contact") return "contact";
  return "none";
}

/**
 * Chip kind for the bucket badge. Sensitive-data reads alarm (`auth`) only
 * when a floor category is present, else calm `cyan`; the other two buckets
 * stay `neutral`. Alarm color is reserved for the catastrophic floor.
 */
export function bucketChipKind(
  bucket: RefusalBucket,
  floor: boolean,
): "auth" | "cyan" | "neutral" {
  if (bucket === "sensitive") return floor ? "auth" : "cyan";
  return "neutral";
}

/** Tally of refusals per bucket — drives the filter-segment counts. Computed
 * over the FULL loaded set so the counts don't shift as the operator filters. */
export function bucketCounts(
  entries: readonly Pick<AuditRow, "refusal_reason">[],
): Record<RefusalBucket, number> {
  const counts: Record<RefusalBucket, number> = { sensitive: 0, unsafe: 0, integrity: 0 };
  for (const e of entries) counts[refusalBucket(e.refusal_reason)] += 1;
  return counts;
}

/** Filter entries to one bucket (order-preserving). `null` = no filter. Pure. */
export function filterByBucket(
  entries: readonly RefusalEntry[],
  bucket: RefusalBucket | null,
): readonly RefusalEntry[] {
  if (bucket === null) return entries;
  return entries.filter((e) => refusalBucket(e.refusal_reason) === bucket);
}

const HOUR_MS = 60 * 60 * 1000;

/** The protective stat-strip tally. Every number is derived from the rows
 * actually in view — never seeded, never projected. */
export interface RefusalSummary {
  /** Refusals in the trailing 60 minutes. */
  lastHour: number;
  /** Refusals since local midnight. */
  today: number;
  /** Refusals that touched a catastrophic-floor category. */
  floorHits: number;
}

/**
 * Tally the stat strip over the rows in view. `nowMs` is injectable so the
 * windows are deterministic under test. Time windows are computed over the
 * loaded (newest-first) page; once the feed is windowed (total > the loaded
 * page) the caller discloses that the counts cover the loaded rows rather than
 * presenting them as all-time absolutes (see Timeline's window note). A
 * future/skewed timestamp never counts toward `lastHour` (guarded
 * non-negative), matching `formatRelativeTime`.
 */
export function summarizeRefusals(
  entries: readonly Pick<AuditRow, "occurred_at" | "pii_categories">[],
  nowMs: number = Date.now(),
): RefusalSummary {
  const startOfDay = new Date(nowMs);
  startOfDay.setHours(0, 0, 0, 0);
  const midnightMs = startOfDay.getTime();

  let lastHour = 0;
  let today = 0;
  let floorHits = 0;
  for (const e of entries) {
    const t = Date.parse(e.occurred_at);
    if (!Number.isNaN(t)) {
      const age = nowMs - t;
      if (age >= 0 && age <= HOUR_MS) lastHour += 1;
      if (t >= midnightMs) today += 1;
    }
    if (isFloorRefusal(e)) floorHits += 1;
  }
  return { lastHour, today, floorHits };
}

/**
 * Category-aware recovery hint — the same deterministic guidance the agent
 * itself receives, derived from the blocked categories (NOT a fabricated
 * narrative). Surfaced only inside the expanded envelope detail, never on the
 * resting row. Mirrors the engine's per-category recovery advice.
 */
const RECOVERY_HINTS: Readonly<Record<PIICategory, string>> = {
  credential:
    "Drop credential columns (password_hash, api_key, secret, token) from the projection and retry.",
  payment_card:
    "Drop card_number / cvv columns from the projection. card_brand and card_exp_* are safe.",
  government_id:
    "Drop ssn / passport / tax_id columns from the projection. Use the entity's surrogate id instead.",
  contact:
    "Drop email / phone / address columns, or aggregate (e.g. count distinct) so individuals are not identifiable.",
  health:
    "Drop diagnosis / procedure / medical-record columns. Health data needs explicit operator authorisation.",
  genetic:
    "Drop genetic_marker / dna_sequence columns. Genetic data is non-aggregable and usually requires explicit consent.",
  biometric:
    "Drop biometric_template / face_embedding / voice_print columns. Biometric data is not aggregable.",
  behavioral:
    "Drop click_path / session_replay / behavioral_event columns. Aggregate at the cohort level instead.",
  online_identifier:
    "Drop cookie_id / device_id / advertising_id columns. Server-side surrogate ids are safer.",
  financial:
    "Drop balance / transaction_amount / income columns at row level. Use aggregated metrics instead.",
  location:
    "Drop precise lat/lng / address columns. Coarse-grained location (city, country) may be acceptable.",
  demographic_protected:
    "Drop race / religion / sexual_orientation columns. Protected-class attributes need explicit operator policy.",
};

/** Build the recovery hint for a refusal from its blocked categories. Falls
 * back to a generic projection/policy line when no category is present (the
 * non-PII buckets). */
export function buildRecoveryHint(categories: readonly PIICategory[]): string {
  if (categories.length === 0) {
    return "Drop the blocked columns from the projection or widen the policy in your host config.";
  }
  return categories.map((c) => RECOVERY_HINTS[c]).join(" ");
}
