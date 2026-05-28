"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { HeaderStrip } from "@/components/dashboard/HeaderStrip";
import { HalfCircleIcon } from "@/components/icons/HalfCircleIcon";
import type { PIICategory } from "@/lib/types";
import styles from "@/components/refusals/ledger.module.css";

// Category-aware recovery hints — derived from the actual PII
// categories that triggered the refusal rather than the prior
// hardcoded "remove password_hash or payment details" string (which
// was wrong for contact-only or government_id refusals).
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

function buildRecoveryHint(categories: readonly PIICategory[]): string {
  if (categories.length === 0) {
    return "Drop the blocked columns from the projection or widen the policy in your host config.";
  }
  return categories.map((c) => RECOVERY_HINTS[c]).join(" ");
}

export default function RefusalsPage() {
  const refusalsQuery = useQuery({
    queryKey: ["audit-refusals"],
    queryFn: () => api.auditRefusals(),
  });

  const [selectedId, setSelectedId] = useState<number | null>(null);

  // Second query: fetch the full envelope (reconstructed by the
  // sidecar from the row's structured columns) when a row is
  // selected. The LIST endpoint omits the envelope; only the DETAIL
  // endpoint reconstructs it. Falls through cleanly when no row is
  // selected (enabled: false).
  const refusalDetailQuery = useQuery({
    queryKey: ["audit-refusal-detail", selectedId],
    queryFn: () =>
      selectedId !== null ? api.auditRefusalDetail(selectedId) : null,
    enabled: selectedId !== null,
  });

  const renderContent = () => {
    if (refusalsQuery.isPending) {
      return (
        <div className={styles.shell}>
          <p className="font-mono text-xs text-(--text-muted) animate-pulse">loading security incident feed…</p>
        </div>
      );
    }

    if (refusalsQuery.isError) {
      return (
        <div className={styles.shell}>
          <div className={styles.banner} style={{ borderColor: "var(--color-signal-red)" }}>
            <div className={styles.bannerText}>
              <h2>Failed to Load Security Feed</h2>
              <p>{refusalsQuery.error.message}</p>
            </div>
          </div>
        </div>
      );
    }

    const { items: incidents, total } = refusalsQuery.data;
    const activeId = selectedId ?? (incidents.length > 0 ? incidents[0].id : null);
    const activeIncident = incidents.find((inc) => inc.id === activeId);

    return (
      <div className={styles.shell}>
        {/* Banner summary bar */}
        <div className={styles.banner}>
          <div className={styles.bannerText}>
            <h2>Active Threat Telemetry Feed</h2>
            <p>
              {total === 0
                ? "No security refusals recorded. The LLM agent has generated valid SQL requests."
                : `${total} refused tool executions recorded across categorized PII columns.`}
            </p>
          </div>
          <Link href="/" className={styles.backBtn}>
            ← dashboard
          </Link>
        </div>

        {incidents.length === 0 ? (
          <div className={styles.noIncident}>
            <HalfCircleIcon
              className="text-(--color-signal-green) w-10 h-10 mb-4"
              aria-label="Ledger secure"
            />
            <h3 className={styles.noIncidentTitle}>Ledger is Secure</h3>
            <p className={styles.noIncidentText}>
              SchemaBrain hasn’t blocked any LLM tool requests yet. This means all executed SQL statement structures successfully passed security audits.
            </p>
          </div>
        ) : (
          <div className={styles.layout}>
            {/* Left Column: List of Incidents */}
            <div className={styles.listColumn}>
              <h3 className={styles.listHeader}>Incidents ({incidents.length})</h3>
              {incidents.map((incident) => {
                const isSelected = incident.id === activeId;
                const timeStr = new Date(incident.occurred_at).toLocaleTimeString();
                return (
                  <article
                    key={incident.id}
                    className={`${styles.card} ${isSelected ? styles.activeCard : ""}`}
                    onClick={() => setSelectedId(incident.id)}
                    role="button"
                    tabIndex={0}
                    aria-selected={isSelected}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedId(incident.id);
                      }
                    }}
                  >
                    <div className={styles.cardHeader}>
                      <span className={styles.badge}>{incident.refusal_reason ?? "pii_blocked"}</span>
                      <time className={styles.cardTime}>{timeStr}</time>
                    </div>
                    <h4 className={styles.cardTitle}>{incident.tool_name}</h4>
                    <p className={styles.cardSubtitle}>
                      ID: #{incident.id} · {incident.pii_categories.join(", ") || "unspecified"}
                    </p>
                  </article>
                );
              })}
            </div>

            {/* Right Column: Detailed View */}
            <div className={styles.detailColumn}>
              {activeIncident ? (
                <div className={styles.detailCard}>
                  <header className={styles.detailHeader}>
                    <div>
                      <h3 className={styles.detailHeaderTitle}>
                        Refusal Event: {activeIncident.tool_name}
                      </h3>
                      <div className={styles.detailMetaList}>
                        <span>Incident: #{activeIncident.id}</span>
                        <span>·</span>
                        <span>Time: {new Date(activeIncident.occurred_at).toLocaleString()}</span>
                        <span>·</span>
                        <span>Source: {activeIncident.source_connection_id}</span>
                      </div>
                    </div>
                  </header>

                  <h4 className={styles.sectionTitle}>Blocked Column Sensitivity</h4>
                  <div className={styles.categoryTagList}>
                    {activeIncident.pii_categories.map((cat) => (
                      <span key={cat} className={`${styles.categoryTag} flex items-center gap-1 inline-flex`}>
                        <HalfCircleIcon className="text-(--color-signal-red) w-3.5 h-3.5" />
                        <span>{cat.replace(/_/g, " ")}</span>
                      </span>
                    ))}
                  </div>

                  <h4 className={styles.sectionTitle}>Structured Security Refusal</h4>
                  <div className={styles.explanation}>
                    <p>
                      SchemaBrain intercepted an LLM request invoking the tool{" "}
                      <strong>`{activeIncident.tool_name}`</strong>. The query accessed data fields tagged with{" "}
                      <strong>
                        {activeIncident.pii_categories.map((c) => `'${c}'`).join(", ")}
                      </strong>{" "}
                      sensitivity categories. By default, SchemaBrain’s firewall blocks these request patterns to prevent catastrophic leaks, returning structured recovery hints that let LLMs self-correct.
                    </p>
                  </div>

                  <h4 className={styles.sectionTitle}>Why this was blocked</h4>
                  <div className={styles.specList}>
                    <span className={styles.specLabel}>Refusal Reason:</span>
                    <span className={styles.specValue}>{activeIncident.refusal_reason ?? "pii_blocked"}</span>

                    <span className={styles.specLabel}>Recovery Hint:</span>
                    <span className={styles.specValue}>
                      {buildRecoveryHint(activeIncident.pii_categories)}
                    </span>

                    <span className={styles.specLabel}>Cost Class:</span>
                    <span className={styles.specValue} style={{ color: "var(--color-signal-red)", fontWeight: "bold" }}>
                      {activeIncident.cost_class}
                    </span>
                  </div>

                  <h4 className={styles.sectionTitle}>Audit row hashes</h4>
                  <div className={styles.specList}>
                    <span className={styles.specLabel}>Fingerprint Hash:</span>
                    <span className={styles.specValue}>{activeIncident.fingerprint_hex}</span>

                    <span className={styles.specLabel}>Ledger Link Hash:</span>
                    <span className={styles.specValue}>{activeIncident.chain_hash_hex}</span>
                  </div>

                  <h4 className={styles.sectionTitle}>Reconstructed Envelope Payload</h4>
                  <div className={styles.codePanel}>
                    <div className={styles.codeHeader}>
                      <span className={styles.codeTitle}>Raw Tool Response JSON</span>
                      <span className="font-mono text-[10px] text-emerald-500 uppercase tracking-widest">
                        {refusalDetailQuery.isPending
                          ? "// loading from sidecar..."
                          : "// sidecar-reconstructed (charter v1.2)"}
                      </span>
                    </div>
                    <pre className={styles.codeArea}>
                      {refusalDetailQuery.data
                        ? JSON.stringify(
                            refusalDetailQuery.data.envelope ?? {
                              status: "refused",
                              error: {
                                kind: activeIncident.refusal_reason ?? "pii_blocked",
                                pii_categories: activeIncident.pii_categories,
                              },
                            },
                            null,
                            2,
                          )
                        : "Loading reconstructed envelope from sidecar…"}
                    </pre>
                  </div>
                </div>
              ) : (
                <div className={styles.detailCard}>
                  <p className="font-mono text-sm text-(--text-muted) text-center">Select an incident to view details.</p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <main className="relative min-h-screen bg-(--surface-base) overflow-hidden pb-16">
      {/* Visual background grid backdrop */}
      <div className="absolute inset-0 grid-bg pointer-events-none" />

      {/* Decorative background radial glows */}
      <div className="absolute top-[-10%] left-[-10%] w-[50%] h-[50%] bg-[#3ECF8E]/8 rounded-full blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[50%] h-[50%] bg-[#3ECF8E]/4 rounded-full blur-[120px] pointer-events-none" />

      <div className="relative z-10">
        <HeaderStrip surface="refusal experience" />
        {renderContent()}
      </div>
    </main>
  );
}
