"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AuditRow as AuditRowT, MerkleProofResponse } from "@/lib/types";
import { verifyProofAgainstRoot } from "@/lib/merkle";
import { formatRelativeTime } from "@/lib/relativeTime";
import { Eyebrow, Icon, PiiChip, useToast } from "@/components/kit";
import styles from "./ledger.module.css";

/** Per-row verification state, driven by the Ledger's "Verify against root"
 * pass. `idle` is the resting state (not yet checked). `edited` is the tamper
 * signal — the server chain walk found this row's stored hash no longer matches
 * its contents. `broken` is the rarer internal inconsistency (the inclusion
 * proof did not reconcile to the root). */
export type RowVerifyState =
  | "idle"
  | "checking"
  | "proven"
  | "edited"
  | "broken"
  | "advanced"
  | "unavailable";

interface AuditRowProps {
  row: AuditRowT;
  expanded: boolean;
  onToggle: () => void;
  /** Injected "now" so the relative time renders deterministically. */
  nowMs: number;
  /** 0-based leaf index in the tree (oldest = 0). Authoritative value comes
   * from the proof during a verify pass; otherwise the resting estimate. */
  leafIndex: number | null;
  /** This row's state in the current verify pass. */
  proofState: RowVerifyState;
  /** The global Merkle root the expand ladder reconciles against (null while
   * the root query is still loading). */
  rootHex: string | null;
}

const HASH_SHOWN = 12;

function formatClock(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "—";
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const PROOF_LABEL: Record<RowVerifyState, string> = {
  idle: "—",
  checking: "proving",
  proven: "proven",
  edited: "edited",
  broken: "no proof",
  advanced: "log grew",
  unavailable: "missing",
};

const PROOF_ICON: Partial<Record<RowVerifyState, "loader" | "check" | "x">> = {
  checking: "loader",
  proven: "check",
  edited: "x",
  broken: "x",
};

/** Spoken clause appended to a row's accessible name for its settled state. */
const PROOF_ANNOUNCE: Record<RowVerifyState, string> = {
  idle: "",
  checking: ", verifying",
  proven: ", proven against root",
  edited: ", edited after write",
  broken: ", proof did not reconcile",
  advanced: ", log advanced",
  unavailable: ", proof unavailable",
};

export function AuditRow({
  row,
  expanded,
  onToggle,
  nowMs,
  leafIndex,
  proofState,
  rootHex,
}: AuditRowProps) {
  const refused = row.status === "refused";
  const epochSeconds = Math.floor(Date.parse(row.occurred_at) / 1000);
  const ago = Number.isNaN(epochSeconds) ? "—" : formatRelativeTime(epochSeconds, nowMs);
  const regionId = `audit-proof-${row.id}`;

  const flagged = proofState === "broken" || proofState === "edited";
  const rowClass = [styles.row, flagged ? styles.rowBroken : "", expanded ? styles.rowOpen : ""]
    .filter(Boolean)
    .join(" ");

  // A sentence accessible name — the concatenated cell text otherwise reads as
  // ambiguous tokens. The visual cells stay; this gives AT a coherent summary,
  // including the settled verify outcome so the per-row result is reachable
  // without forcing an expand (the color/icon coding covers sighted users).
  const rowLabel =
    `Audit row ${row.id}, ${row.tool_name}, ${ago}` +
    (refused ? ", refused" : "") +
    PROOF_ANNOUNCE[proofState] +
    `. ${expanded ? "Hide" : "Show"} inclusion proof.`;

  const icon = PROOF_ICON[proofState];

  return (
    <li className={styles.rowWrap}>
      <button
        type="button"
        className={rowClass}
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={expanded ? regionId : undefined}
        aria-label={rowLabel}
      >
        {/* time */}
        <span className={styles.timeCell}>
          <time className={styles.timeAgo} dateTime={row.occurred_at}>
            {ago}
          </time>
          <span className={styles.timeClock}>{formatClock(row.occurred_at)}</span>
        </span>

        {/* row id */}
        <span className={styles.idCell}>#{row.id}</span>

        {/* tool · caller */}
        <span className={styles.toolCell}>
          <span className={styles.toolName}>{row.tool_name}()</span>
          <PiiChip kind="neutral" className={styles.callerChip}>
            <Icon name="database" size={11} />
            <span className={styles.callerName}>{row.caller_id ?? "agent"}</span>
          </PiiChip>
        </span>

        {/* cost class — refused rows show the refused marker, not a cost pill */}
        {refused ? (
          <span className={styles.costChip}>refused</span>
        ) : (
          <span className={styles.costChip} data-cost={row.cost_class}>
            {row.cost_class}
          </span>
        )}

        {/* chain hash */}
        <span className={`${styles.hashCell} ${refused ? styles.hashCellRefused : ""}`}>
          <Icon name={refused ? "ban" : "link"} size={12} className={styles.hashIcon} />
          <span className={styles.hashText}>0x{row.chain_hash_hex.slice(0, HASH_SHOWN)}…</span>
        </span>

        {/* idx (leaf_index) */}
        <span className={styles.idxCell}>{leafIndex === null ? "—" : leafIndex}</span>

        {/* proof state + chevron */}
        <span className={styles.proofCell}>
          <span className={styles.proofState} data-state={proofState}>
            {icon && (
              <Icon name={icon} size={12} className={proofState === "checking" ? styles.spin : undefined} />
            )}
            <span>{PROOF_LABEL[proofState]}</span>
          </span>
          <Icon
            name="chevron-down"
            size={14}
            className={`${styles.chevron} ${expanded ? styles.chevronOpen : ""}`}
          />
        </span>
      </button>

      {expanded && (
        <ProofDetail row={row} regionId={regionId} rootHex={rootHex} edited={proofState === "edited"} />
      )}
    </li>
  );
}

/* ─────────── expanded inclusion-proof detail ─────────── */

type LadderVerdict = "pending" | "ok" | "fail" | "advanced";

function ProofDetail({
  row,
  regionId,
  rootHex,
  edited,
}: {
  row: AuditRowT;
  regionId: string;
  rootHex: string | null;
  /** The verify pass flagged this row's stored chain hash as no longer
   * matching its contents — it was edited after it was written. */
  edited: boolean;
}) {
  const { copyToClipboard } = useToast();
  const proofQuery = useQuery({
    queryKey: ["audit-row-proof", row.id],
    queryFn: () => api.auditRowProof(row.id),
  });
  const proof = proofQuery.data;

  const [verdict, setVerdict] = useState<LadderVerdict>("pending");
  useEffect(() => {
    if (!proof || !rootHex) return;
    if (proof.root_hex !== rootHex) {
      setVerdict("advanced");
      return;
    }
    let cancelled = false;
    setVerdict("pending");
    void verifyProofAgainstRoot(proof, rootHex).then((ok) => {
      if (!cancelled) setVerdict(ok ? "ok" : "fail");
    });
    return () => {
      cancelled = true;
    };
  }, [proof, rootHex]);

  const payloadJson = JSON.stringify(
    {
      id: row.id,
      occurred_at: row.occurred_at,
      source_connection_id: row.source_connection_id,
      tool_name: row.tool_name,
      status: row.status,
      refusal_reason: row.refusal_reason,
      cost_class: row.cost_class,
      pii_categories: row.pii_categories,
      fingerprint_hex: row.fingerprint_hex,
      fingerprint_version: row.fingerprint_version,
      chain_hash_hex: row.chain_hash_hex,
    },
    null,
    2,
  );

  return (
    <div id={regionId} role="region" aria-label={`audit row ${row.id} inclusion proof`} className={styles.expandRegion}>
      <div className={styles.expandInner}>
        {edited && (
          <div className={`${styles.banner} ${styles.bannerBroken}`} role="alert">
            <span className={styles.bannerIcon} aria-hidden="true">
              <Icon name="x" size={18} />
            </span>
            <span>
              This row’s stored chain hash no longer matches its contents — it was edited after it
              was written. The inclusion proof below still folds to the current root because the root
              recomputes from the rows on disk; an archived earlier root is what catches a change.
            </span>
          </div>
        )}

        {/* inclusion proof ladder */}
        <section className={styles.detailSection}>
          <Eyebrow>Inclusion proof · recomputed in your browser</Eyebrow>
          <p className={styles.detailCaption}>
            Each rung folds the row up the Merkle tree with SHA-256. The browser recomputes the
            root from the leaf hash and these siblings, then checks it against the root above.
          </p>
          {proofQuery.isPending ? (
            <p className={styles.skeleton} role="status">
              fetching proof…
            </p>
          ) : proofQuery.isError ? (
            <p className={styles.detailCaption}>
              Couldn’t fetch this row’s proof from the sidecar — check the terminal logs.
            </p>
          ) : proof ? (
            <ProofLadder proof={proof} verdict={verdict} />
          ) : null}
        </section>

        {/* audit row — the tamper-evident chain link */}
        <section className={styles.detailSection}>
          <Eyebrow>Audit row · tamper-evident chain</Eyebrow>
          <div className={styles.specList}>
            {proof && (
              <>
                <span className={styles.specLabel}>leaf index</span>
                <span className={styles.specValue}>
                  {proof.leaf_index} of {proof.tree_size}
                </span>
              </>
            )}

            <span className={styles.specLabel}>source</span>
            <span className={styles.specValue}>{row.source_connection_id}</span>

            <span className={styles.specLabel}>fingerprint</span>
            <button
              type="button"
              className={styles.copyHash}
              onClick={() => copyToClipboard(row.fingerprint_hex, "fingerprint copied")}
              aria-label={`Copy fingerprint ${row.fingerprint_hex}`}
            >
              {row.fingerprint_hex}
            </button>

            <span className={styles.specLabel}>chain hash</span>
            <button
              type="button"
              className={styles.copyHash}
              onClick={() => copyToClipboard(row.chain_hash_hex, "chain hash copied")}
              aria-label={`Copy chain hash ${row.chain_hash_hex}`}
            >
              {row.chain_hash_hex}
            </button>
          </div>
        </section>

        {/* reconstructed payload */}
        <section className={styles.detailSection}>
          <div className={styles.detailHead}>
            <Eyebrow>Row payload</Eyebrow>
            <button
              type="button"
              className={styles.copyHash}
              onClick={() => copyToClipboard(payloadJson, "payload copied")}
            >
              <Icon name="copy" size={13} /> copy
            </button>
          </div>
          <p className={styles.detailCaption}>
            The structured columns hashed into this row — the original request text is never stored.
          </p>
          <div className={styles.codePanel}>
            <div className={styles.codeHeader}>
              <span className={styles.codeTitle}>audit row {row.id}</span>
            </div>
            <pre className={styles.codeArea}>{payloadJson}</pre>
          </div>
        </section>
      </div>
    </div>
  );
}

/* ─────────── the proof fold ladder (graft B) ─────────── */

function shortHash(hex: string): string {
  return hex.length <= 16 ? hex : `${hex.slice(0, 10)}…${hex.slice(-6)}`;
}

const VERDICT_COPY: Record<LadderVerdict, { text: string; icon: "check" | "x" | "loader" }> = {
  pending: { text: "recomputing…", icon: "loader" },
  ok: { text: "matches the root", icon: "check" },
  fail: { text: "does not reconcile to the root", icon: "x" },
  advanced: { text: "log advanced since the root was read — re-verify", icon: "loader" },
};

function ProofLadder({ proof, verdict }: { proof: MerkleProofResponse; verdict: LadderVerdict }) {
  const okFlag = verdict === "ok" ? "true" : verdict === "fail" ? "false" : "pending";
  const v = VERDICT_COPY[verdict];

  return (
    <div className={styles.ladder} aria-label="inclusion proof ladder">
      <div className={`${styles.ladderRung} ${styles.ladderLeaf}`}>
        <span className={styles.ladderRole}>leaf {proof.leaf_index}</span>
        <span>
          <span className={styles.ladderHash}>{shortHash(proof.leaf_hash_hex)}</span>
          <br />
          <span className={styles.ladderNote} aria-hidden="true">
            sha256(0x00 ‖ row)
          </span>
        </span>
      </div>
      {proof.audit_path.length === 0 ? (
        <div className={styles.ladderRung}>
          <span className={styles.ladderRole}>sole row</span>
          <span className={styles.ladderNote}>this row is the whole tree — the leaf is the root</span>
        </div>
      ) : (
        proof.audit_path.map((sibling, i) => (
          <div className={styles.ladderRung} key={`${sibling}-${i}`}>
            <span className={styles.ladderRole}>+ sibling</span>
            <span>
              <span className={styles.ladderHash}>{shortHash(sibling)}</span>
              <br />
              <span className={styles.ladderNote} aria-hidden="true">
                sha256(0x01 ‖ … ‖ …)
              </span>
            </span>
          </div>
        ))
      )}
      <div className={styles.ladderVerdict} data-ok={okFlag}>
        <Icon name={v.icon} size={14} className={verdict === "pending" ? styles.spin : undefined} />
        <span>
          = root {shortHash(proof.root_hex)} · {v.text}
        </span>
      </div>
    </div>
  );
}
