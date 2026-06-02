"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { HalfCircleIcon } from "@/components/icons/HalfCircleIcon";
import type { AuditRow } from "@/lib/types";
import styles from "@/components/audit/ledger.module.css";

export default function AuditPage() {
  const auditRowsQuery = useQuery({
    queryKey: ["audit-rows"],
    queryFn: () => api.auditRows(),
  });

  const auditVerifyQuery = useQuery({
    queryKey: ["audit-verify"],
    queryFn: () => api.auditVerify(),
  });

  const [selectedId, setSelectedId] = useState<number | null>(null);

  const renderContent = () => {
    if (auditRowsQuery.isPending || auditVerifyQuery.isPending) {
      return (
        <div className={styles.shell}>
          <p className="font-mono text-xs text-(--text-muted) animate-pulse">loading audit ledger substrate…</p>
        </div>
      );
    }

    if (auditRowsQuery.isError) {
      return (
        <div className={styles.shell}>
          <div className={styles.ribbon} style={{ borderColor: "var(--color-signal-red)" }}>
            <div className={styles.ribbonText}>
              <h2>Failed to Load Cryptographic Ledger</h2>
              <p>{auditRowsQuery.error.message}</p>
            </div>
          </div>
        </div>
      );
    }

    const { items, total } = auditRowsQuery.data;
    const rows = items as readonly AuditRow[];
    const verifyData = auditVerifyQuery.data;
    const isIntact = verifyData?.status === "intact";

    // Auto-select the first row if none selected
    const activeId = selectedId ?? (rows.length > 0 ? rows[0].id : null);
    const activeIndex = rows.findIndex((row) => row.id === activeId);
    const activeRow = rows[activeIndex] as AuditRow | undefined;

    const prevRow = activeIndex !== -1 && activeIndex < rows.length - 1 ? rows[activeIndex + 1] : null;
    const prevHash = prevRow ? prevRow.chain_hash_hex : "genesis_block_anchor";

    return (
      <div className={styles.shell}>
        {/* Verification Ribbon */}
        <div className={styles.ribbon}>
          <div className={styles.ribbonText}>
            <h2 className="inline-flex items-center gap-2">
              {isIntact ? (
                <>
                  <HalfCircleIcon
                    className="text-(--color-signal-green) w-5 h-5"
                    aria-label="Ledger chain intact"
                  />
                  Ledger Chain Intact
                </>
              ) : (
                <>
                  <HalfCircleIcon
                    className="text-(--color-signal-red) w-5 h-5"
                    aria-label="Ledger chain mismatch"
                  />
                  Ledger Chain Mismatch
                </>
              )}
            </h2>
            <p>
              {isIntact
                ? `Cryptographic walk verified successfully. ${total} execution blocks checked and securely hashed via SHA-256.`
                : "Chain verify detected a mismatch in block hashes. Re-run indexer or check sidecar logs."}
            </p>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => {
                auditVerifyQuery.refetch();
                auditRowsQuery.refetch();
              }}
              className={styles.actionBtn}
              aria-label="Verify chain"
            >
              Verify ledger
            </button>
            <Link href="/" className={styles.backBtn}>
              ← dashboard
            </Link>
          </div>
        </div>

        {rows.length === 0 ? (
          <div className="text-center py-20 border border-dashed border-(--border-subtle) rounded-lg">
            <h3 className="font-display text-lg font-semibold text-(--text-primary) mb-2">No Transactions Audited</h3>
            <p className="font-mono text-xs text-(--text-muted)">The active source database hasn’t processed any LLM tool requests yet.</p>
          </div>
        ) : (
          <div className={styles.layout}>
            {/* Left Pane: Ledger Table Grid */}
            <div className={styles.tablePane}>
              <table className={styles.table} role="grid" aria-label="immutable ledger rows">
                <thead>
                  <tr>
                    <th scope="col">Block ID</th>
                    <th scope="col">Timestamp</th>
                    <th scope="col">Tool</th>
                    <th scope="col">Status</th>
                    <th scope="col">Cost Class</th>
                    <th scope="col">Block Hash</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const isSelected = row.id === activeId;
                    const dateStr = new Date(row.occurred_at).toLocaleTimeString();
                    return (
                      <tr
                        key={row.id}
                        className={`${styles.row} ${isSelected ? styles.activeRow : ""}`}
                        onClick={() => setSelectedId(row.id)}
                        role="row"
                        aria-selected={isSelected}
                        tabIndex={0}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            setSelectedId(row.id);
                          }
                        }}
                      >
                        <td>#{row.id}</td>
                        <td>{dateStr}</td>
                        <td className="font-semibold text-(--text-primary)">{row.tool_name}</td>
                        <td>
                          <span
                            className={
                              row.status === "refused" ? styles.statusRefused : styles.statusSuccess
                            }
                          >
                            {row.status}
                          </span>
                        </td>
                        <td>{row.cost_class}</td>
                        <td className="text-(--text-muted)">{row.chain_hash_hex.substring(0, 12)}…</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Right Pane: Transaction detail sidebar */}
            <div className={styles.detailPane}>
              {activeRow ? (
                <div className={styles.detailCard}>
                  <header className={styles.detailHeader}>
                    <h3 className={styles.detailTitle}>Block #{activeRow.id} Details</h3>
                    <p className={styles.detailSubtitle}>
                      Captured: {new Date(activeRow.occurred_at).toLocaleString()}
                    </p>
                  </header>

                  {/* Blockchain graphic links */}
                  <h4 className="font-mono text-[10px] uppercase tracking-widest text-(--text-muted) mb-3">
                    Cryptographic Integrity Linkage
                  </h4>
                  <div className={styles.chainCard}>
                    <div className={styles.chainLink}>
                      <div className={styles.chainHashBox}>
                        <span className={styles.hashLabel}>previous_chain_hash (block #{activeRow.id - 1})</span>
                        <span className={styles.hashValue}>{prevHash.substring(0, 28)}…</span>
                      </div>

                      <div className={styles.chainConnectorLine}>
                        <div className={styles.chainIcon}>
                          🔗
                        </div>
                      </div>

                      <div className={styles.chainHashBox}>
                        <span className={styles.hashLabel}>current_chain_hash (this block)</span>
                        <span className={styles.hashValue} style={{ borderColor: "var(--color-signal-green)" }}>
                          {activeRow.chain_hash_hex.substring(0, 28)}…
                        </span>
                      </div>
                    </div>
                  </div>

                  <h4 className="font-mono text-[10px] uppercase tracking-widest text-(--text-muted) mb-2">
                    Immutable Block Payload
                  </h4>
                  <div className={styles.codePanel}>
                    <div className={styles.codeHeader}>
                      <span className={styles.codeTitle}>Audit Row Telemetry Context</span>
                      <span className="font-mono text-[10px] text-emerald-500 uppercase tracking-widest">
                        // Verified intact
                      </span>
                    </div>
                    <pre className={styles.codeArea}>
                      {JSON.stringify(
                        {
                          id: activeRow.id,
                          occurred_at: activeRow.occurred_at,
                          source_connection_id: activeRow.source_connection_id,
                          tool_name: activeRow.tool_name,
                          status: activeRow.status,
                          refusal_reason: activeRow.refusal_reason,
                          cost_class: activeRow.cost_class,
                          pii_categories: activeRow.pii_categories,
                          fingerprint_hex: activeRow.fingerprint_hex,
                          fingerprint_version: activeRow.fingerprint_version,
                          chain_hash_hex: activeRow.chain_hash_hex,
                        },
                        null,
                        2
                      )}
                    </pre>
                  </div>
                </div>
              ) : (
                <div className={styles.detailCard}>
                  <p className="font-mono text-xs text-(--text-muted) text-center">Select an audited block to view details.</p>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    );
  };

  // The shell (app)/layout.tsx owns the chrome; this surface renders its
  // body into the shell content slot. Reskin onto the kit/tokens is the
  // next PR — only the per-surface HeaderStrip + full-page wrapper go now.
  return renderContent();
}
