"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AuditRow as AuditRowT, MerkleProofResponse } from "@/lib/types";
import { verifyProofAgainstRoot } from "@/lib/merkle";
import { decideVerifyPhase, type VerifyPhase } from "@/lib/auditVerdict";
import { Button, Icon, useToast } from "@/components/kit";
import { AuditRow, type RowVerifyState } from "./AuditRow";
import styles from "./ledger.module.css";

/**
 * THE PROOF — the Audit integrity surface (/audit), mounted inside the app
 * shell (the shell owns the chrome). The instrument-grid sibling of the /pii
 * heatmap and the /refusals ledger: one append-only run of rows down a shared
 * grid, newest-first, spined by the derived Merkle root + a REAL "Verify
 * against root" action.
 *
 * The verify pass runs two complementary checks:
 *   1. The server hash-chain walk over the WHOLE log (the tamper tripwire): does
 *      each row's stored chain hash still match its recomputed value? Its result
 *      drives the global integrity verdict, so a row edited outside the visible
 *      window still trips the alarm.
 *   2. A per-row RFC-6962 inclusion proof, recomputed in the browser with
 *      crypto.subtle, reconciling each VISIBLE row to the one global Merkle root
 *      (`proven`) — the membership proof, and the root you archive off-host.
 *
 * Honesty: the chain is detection, not prevention. The root recomputes from the
 * rows on disk, so it only catches a *coherent* rewrite when compared to an
 * archived earlier root — the archive note + the edited-row alert say so.
 * Read-only; audit rows are global, so this surface is not source-scoped.
 */

const STEP_MS = 70;

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

interface VerifyPass {
  phase: VerifyPhase;
  rowStates: Record<number, RowVerifyState>;
  leafIndices: Record<number, number>;
  /** Rows verified in this pass (the visible window). */
  attempted: number;
  /** Edited rows across the WHOLE log (from the chain walk), not just the
   * window — drives the global verdict so an out-of-window tamper still alarms. */
  editedTotal: number;
  run: (items: readonly AuditRowT[]) => void;
}

/** Drives the "Verify against root" pass: fetch the whole-log chain-walk status
 * + the global root, then for each visible row (id-ASC, the leaf order) flag a
 * stored chain mismatch as `edited` or recompute its inclusion proof. */
function useVerifyPass(): VerifyPass {
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<VerifyPhase>("idle");
  const [rowStates, setRowStates] = useState<Record<number, RowVerifyState>>({});
  const [leafIndices, setLeafIndices] = useState<Record<number, number>>({});
  const [attempted, setAttempted] = useState(0);
  const [editedTotal, setEditedTotal] = useState(0);
  const runIdRef = useRef(0);

  // Invalidate any in-flight pass on unmount so no setState fires after teardown.
  useEffect(() => {
    return () => {
      runIdRef.current += 1;
    };
  }, []);

  const run = useCallback(
    (items: readonly AuditRowT[]) => {
      const myRun = (runIdRef.current += 1);
      const live = () => runIdRef.current === myRun;

      setPhase("running");
      setRowStates({});
      setLeafIndices({});
      setAttempted(items.length);
      setEditedTotal(0);

      // Rows arrive id-DESC (render order); verify in id-ASC (leaf) order.
      const ascending = [...items].reverse();
      const step = prefersReducedMotion() ? 0 : STEP_MS;

      void (async () => {
        let editedIds: Set<number>;
        let rootHex: string;
        try {
          const [status, root] = await Promise.all([
            api.auditVerify({ full: true }),
            api.auditMerkleRoot(),
          ]);
          editedIds = new Set(status.mismatches.map((m) => m.row_id));
          rootHex = root.root_hex;
        } catch {
          if (live()) setPhase("idle");
          return;
        }
        if (!live()) return;
        // Whole-log edited count — the global verdict source.
        setEditedTotal(editedIds.size);

        let proven = 0;
        let proofFailed = 0;
        let advanced = 0;
        for (const row of ascending) {
          if (!live()) return;
          setRowStates((s) => ({ ...s, [row.id]: "checking" }));
          if (step) await sleep(step);
          if (!live()) return;

          // The whole-log chain walk found this row's stored hash no longer
          // matches its contents — the tamper signal.
          if (editedIds.has(row.id)) {
            setRowStates((s) => ({ ...s, [row.id]: "edited" }));
            continue;
          }

          let proof: MerkleProofResponse;
          try {
            proof = await api.auditRowProof(row.id);
          } catch {
            if (!live()) return;
            setRowStates((s) => ({ ...s, [row.id]: "unavailable" }));
            continue;
          }
          if (!live()) return;
          setLeafIndices((m) => ({ ...m, [row.id]: proof.leaf_index }));
          // Hydrate the row-proof cache so expanding the row reuses this fetch.
          queryClient.setQueryData(["audit-row-proof", row.id], proof);

          if (proof.root_hex !== rootHex) {
            advanced += 1;
            setRowStates((s) => ({ ...s, [row.id]: "advanced" }));
            continue;
          }

          const ok = await verifyProofAgainstRoot(proof, rootHex);
          if (!live()) return;
          if (ok) {
            proven += 1;
            setRowStates((s) => ({ ...s, [row.id]: "proven" }));
          } else {
            proofFailed += 1;
            setRowStates((s) => ({ ...s, [row.id]: "broken" }));
          }
        }
        if (!live()) return;
        // Verdict precedence lives in a pure, unit-tested helper. editedTotal is
        // the WHOLE-LOG chain-walk count, so an edited row outside the visible
        // window still trips `broken` (the false-green regression guard).
        setPhase(
          decideVerifyPhase({
            editedTotal: editedIds.size,
            proofFailed,
            advanced,
            proven,
            attempted: items.length,
          }),
        );
      })();
    },
    [queryClient],
  );

  return { phase, rowStates, leafIndices, attempted, editedTotal, run };
}

interface VerifyCounts {
  proven: number;
  editedInView: number;
  failed: number;
  unavailable: number;
}

function countStates(rowStates: Record<number, RowVerifyState>): VerifyCounts {
  let proven = 0;
  let editedInView = 0;
  let failed = 0;
  let unavailable = 0;
  for (const state of Object.values(rowStates)) {
    if (state === "proven") proven += 1;
    else if (state === "edited") editedInView += 1;
    else if (state === "broken") failed += 1;
    else if (state === "unavailable") unavailable += 1;
  }
  return { proven, editedInView, failed, unavailable };
}

export function AuditLedger() {
  const [nowMs] = useState(() => Date.now());
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const verify = useVerifyPass();

  const rowsQuery = useQuery({ queryKey: ["audit-rows"], queryFn: () => api.auditRows() });
  const rootQuery = useQuery({ queryKey: ["audit-merkle-root"], queryFn: () => api.auditMerkleRoot() });

  if (rowsQuery.isPending) {
    return (
      <div className={styles.shell}>
        <Header />
        <p className={styles.skeleton} role="status">
          reading the audit log…
        </p>
      </div>
    );
  }

  if (rowsQuery.isError) {
    return (
      <div className={styles.shell}>
        <Header />
        <ErrorCard message={rowsQuery.error.message} />
      </div>
    );
  }

  const { items, total } = rowsQuery.data;
  const rows = items as readonly AuditRowT[];
  const rootHex = rootQuery.data?.root_hex ?? null;
  const treeSize = rootQuery.data?.tree_size ?? total;
  const windowed = rows.length < total;
  const counts = countStates(verify.rowStates);
  const status = integrityStatus(verify.phase, counts, verify.attempted, verify.editedTotal);

  return (
    <div className={styles.shell}>
      <Header />

      {total === 0 ? (
        <EmptyState />
      ) : (
        <>
          <RootBar
            treeSize={treeSize}
            rootHex={rootHex}
            inView={rows.length}
            total={total}
            windowed={windowed}
            status={status}
          />

          <VerifyBar
            phase={verify.phase}
            status={status}
            onVerify={() => verify.run(rows)}
          />

          {verify.phase === "broken" && (
            <BrokenBanner editedTotal={verify.editedTotal} editedInView={counts.editedInView} failed={counts.failed} />
          )}
          {verify.phase === "advanced" && <AdvancedBanner />}
          {verify.phase === "partial" && <PartialBanner unavailable={counts.unavailable} />}

          <div className={styles.ledgerPanel}>
            <div className={styles.ledgerHead} aria-hidden="true">
              <span>time</span>
              <span>row</span>
              <span>tool · caller</span>
              <span>cost</span>
              <span>chain hash</span>
              <span>idx</span>
              <span className={styles.headProof}>proof</span>
            </div>
            <ul className={styles.list} aria-label="audit rows">
              {rows.map((row, i) => (
                <AuditRow
                  key={row.id}
                  row={row}
                  expanded={expandedId === row.id}
                  onToggle={() => setExpandedId((prev) => (prev === row.id ? null : row.id))}
                  nowMs={nowMs}
                  // Resting estimate: the i-th newest row is leaf (total-1-i).
                  // Overridden by the authoritative proof.leaf_index once verified.
                  leafIndex={verify.leafIndices[row.id] ?? (total > 0 ? total - 1 - i : null)}
                  proofState={verify.rowStates[row.id] ?? "idle"}
                  rootHex={rootHex}
                />
              ))}
            </ul>
            <Legend />
          </div>

          {windowed && (
            <p className={styles.archiveNote}>
              The grid shows the {rows.length} most recent of {total} rows; the chain walk and the
              root cover all {total}.
            </p>
          )}
          <ArchiveNote />
        </>
      )}
    </div>
  );
}

/* ─────────── header ─────────── */

function Header() {
  return (
    <>
      <div className={styles.titleStrip}>
        <h1 className={styles.surfaceTitle}>Audit</h1>
        <p className={styles.titleMeta}>append-only · tamper-evident</p>
      </div>
      <p className={styles.lede}>
        Every tool call appends one row that commits to the hash of the row before it. A change to
        any past row breaks every hash after it — and this page recomputes the proof to show it.
      </p>
    </>
  );
}

/* ─────────── integrity status helper ─────────── */

function plural(n: number): string {
  return n === 1 ? "" : "s";
}

type StatusTone = "ink" | "cyan" | "green" | "alarm" | "amber";

interface IntegrityStatus {
  /** Visual short text for the stat strip + verify bar. */
  text: string;
  tone: StatusTone;
  /** Stable per-phase sentence for the screen-reader live region (does not
   * change per row, so the stepping pass never floods AT). */
  announce: string;
}

function integrityStatus(
  phase: VerifyPhase,
  counts: VerifyCounts,
  attempted: number,
  editedTotal: number,
): IntegrityStatus {
  switch (phase) {
    case "running":
      return { text: `proving · ${counts.proven}/${attempted}`, tone: "cyan", announce: "Verifying audit rows." };
    case "done":
      return {
        text: `verified · ${counts.proven}/${attempted} intact`,
        tone: "green",
        announce: `Verified. All ${counts.proven} rows intact and included.`,
      };
    case "broken":
      return editedTotal > 0
        ? {
            text: `${editedTotal} row${plural(editedTotal)} edited after write`,
            tone: "alarm",
            announce: `${editedTotal} row${plural(editedTotal)} edited after they were written.`,
          }
        : {
            text: `${counts.failed} row${plural(counts.failed)} failed proof`,
            tone: "alarm",
            announce: `${counts.failed} row${plural(counts.failed)} failed proof.`,
          };
    case "advanced":
      return {
        text: "log advanced — re-verify",
        tone: "amber",
        announce: "The log advanced during verification. Re-verify.",
      };
    case "partial":
      return {
        text: `${counts.proven}/${attempted} proven · ${attempted - counts.proven} unverifiable`,
        tone: "amber",
        announce: `${counts.proven} of ${attempted} rows verified; ${attempted - counts.proven} could not be checked.`,
      };
    default:
      return { text: "not verified this session", tone: "ink", announce: "" };
  }
}

/* ─────────── root bar (stat strip) ─────────── */

function RootBar({
  treeSize,
  rootHex,
  inView,
  total,
  windowed,
  status,
}: {
  treeSize: number;
  rootHex: string | null;
  inView: number;
  total: number;
  windowed: boolean;
  status: IntegrityStatus;
}) {
  const { copyToClipboard } = useToast();
  const stripState = status.tone === "green" ? "done" : status.tone === "alarm" ? "broken" : undefined;

  return (
    <div className={styles.summaryStrip} data-state={stripState} role="group" aria-label="audit integrity summary">
      <div className={styles.stat}>
        <span className={styles.statNum}>{treeSize}</span>
        <span className={styles.statLabel}>rows in tree</span>
      </div>

      <div className={`${styles.stat} ${styles.rootStat}`}>
        <span className={styles.statLabel}>merkle root</span>
        {rootHex ? (
          <button
            type="button"
            className={styles.rootValue}
            onClick={() => copyToClipboard(rootHex, "merkle root copied")}
            aria-label={`Copy Merkle root ${rootHex}`}
          >
            <span>{rootHex}</span>
            <Icon name="copy" size={12} className={styles.rootCopyIcon} />
          </button>
        ) : (
          <span className={styles.statSub}>computing…</span>
        )}
      </div>

      <div className={styles.stat}>
        <span className={styles.statNum} data-tone={status.tone} style={{ fontSize: "0.9375rem" }}>
          {status.text}
        </span>
        <span className={styles.statLabel}>integrity</span>
      </div>

      <div className={styles.stat}>
        <span className={styles.statNum}>{inView}</span>
        <span className={styles.statLabel}>in view</span>
        {windowed && <span className={styles.statSub}>of {total}</span>}
      </div>
    </div>
  );
}

/* ─────────── verify bar (formula + action) ─────────── */

function VerifyBar({
  phase,
  status,
  onVerify,
}: {
  phase: VerifyPhase;
  status: IntegrityStatus;
  onVerify: () => void;
}) {
  const running = phase === "running";
  const statusState =
    status.tone === "green"
      ? "done"
      : status.tone === "alarm"
        ? "broken"
        : status.tone === "amber"
          ? "advanced"
          : undefined;
  const settled = phase === "done" || phase === "broken" || phase === "advanced" || phase === "partial";
  const label = settled ? "Re-verify against root" : "Verify against root";

  return (
    <div className={styles.verifyBar}>
      <span
        className={styles.formula}
        role="img"
        aria-label="Each row's hash is SHA-256 of the row's data concatenated with the previous row's hash."
      >
        <span aria-hidden="true">
          H(row<sub>n</sub>) = SHA256( <b>data</b>
          <sub>n</sub> ‖ H(row<sub>n−1</sub>) )
        </span>
      </span>
      <span className={styles.verifySpacer} />
      {/* Visual progress (not a live region, so the per-row stepping does not
          flood assistive tech). */}
      <span className={styles.verifyStatus} data-state={statusState} aria-hidden="true">
        {running && <Icon name="loader" size={14} className={styles.spin} />}
        {status.text}
      </span>
      {/* Stable per-phase announcement for screen readers. */}
      <span className={styles.srOnly} role="status" aria-live="polite" aria-atomic="true">
        {status.announce}
      </span>
      <Button variant="primary" icon="shield-check" onClick={onVerify} disabled={running} aria-label={label}>
        {label}
      </Button>
    </div>
  );
}

/* ─────────── banners ─────────── */

function BrokenBanner({
  editedTotal,
  editedInView,
  failed,
}: {
  editedTotal: number;
  editedInView: number;
  failed: number;
}) {
  const outOfView = Math.max(0, editedTotal - editedInView);
  return (
    <div className={`${styles.banner} ${styles.bannerBroken}`} role="alert">
      <span className={styles.bannerIcon} aria-hidden="true">
        <Icon name="x" size={18} />
      </span>
      <span>
        {editedTotal > 0 ? (
          <>
            {editedTotal} row{plural(editedTotal)} stored hash no longer matches its contents —
            edited after it was written.
            {outOfView > 0 && (
              <>
                {" "}
                {outOfView} {outOfView === 1 ? "is" : "are"} outside the current view — run{" "}
                <code>schemabrain audit verify --full</code> to inspect {outOfView === 1 ? "it" : "them"}.
              </>
            )}{" "}
            If you archived an earlier root off-host, compare it to confirm what changed. The chain is
            tamper-evident, not tamper-proof.
          </>
        ) : (
          <>
            {failed} row{plural(failed)} did not reconcile to the root the sidecar reported — an
            internal inconsistency. Check the sidecar logs in your terminal.
          </>
        )}
      </span>
    </div>
  );
}

function AdvancedBanner() {
  return (
    <div className={`${styles.banner} ${styles.bannerAdvanced}`} role="status">
      <span className={styles.bannerIcon} aria-hidden="true">
        <Icon name="loader" size={18} />
      </span>
      <span>
        The log grew while verifying, so some rows were proved against a newer root. Re-verify to
        reconcile every row against the current root.
      </span>
    </div>
  );
}

function PartialBanner({ unavailable }: { unavailable: number }) {
  return (
    <div className={`${styles.banner} ${styles.bannerAdvanced}`} role="status">
      <span className={styles.bannerIcon} aria-hidden="true">
        <Icon name="loader" size={18} />
      </span>
      <span>
        {unavailable} row{plural(unavailable)} could not be located for a proof (it may have been
        compacted away). The chain walk still covered the whole log; re-verify to retry.
      </span>
    </div>
  );
}

/* ─────────── legend + archive note ─────────── */

function Legend() {
  return (
    <div className={styles.legend}>
      <span className={styles.legendKey}>
        <Icon name="link" size={12} /> hashed into the chain
      </span>
      <span className={styles.legendKey}>
        <Icon name="ban" size={12} /> refused (held by definition)
      </span>
      <span className={styles.legendKey}>
        <Icon name="check" size={12} /> proven against root
      </span>
      <span className={styles.legendNote}>request text is never stored — rows show only structured columns</span>
    </div>
  );
}

function ArchiveNote() {
  return (
    <p className={styles.archiveNote}>
      The chain is <strong>tamper-evident, not tamper-proof</strong>: anyone who can write the audit
      file could rewrite the rows and recompute this root. The real defence is to archive the root
      off-host and compare it later — a change to any earlier row will not reproduce the same root.
    </p>
  );
}

/* ─────────── empty / error states ─────────── */

function EmptyState() {
  return (
    <div className={styles.emptyState}>
      <Icon name="shield" size={40} className={styles.emptyIcon} label="nothing audited yet" />
      <h2 className={styles.emptyTitle}>Nothing audited yet.</h2>
      <p className={styles.emptyText}>
        No tool call has been recorded. The first request an agent makes appends the first row — and
        the root will appear here.
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
            <p>The audit log fills in once a source is indexed and an agent makes a request.</p>
            <code>$ schemabrain init</code>
            <code>$ schemabrain index --target postgres://your-replica</code>
          </>
        ) : (
          <>
            <h2>Couldn’t load the audit log.</h2>
            <p>{message}</p>
            <p>
              The sidecar is running, but the audit query failed. Check the sidecar logs in your
              terminal for the underlying cause.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
