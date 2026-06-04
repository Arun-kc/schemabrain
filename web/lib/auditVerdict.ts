// Pure decision logic for the /audit "Verify against root" pass — extracted
// from the component so the verdict precedence is unit-testable in isolation
// (no React, no fetches). The load-bearing rule: the global integrity verdict
// is driven by the WHOLE-LOG chain walk (`editedTotal`), NOT by the visible
// window — so a row edited outside the 50-row page still trips `broken`.

export type VerifyPhase = "idle" | "running" | "done" | "broken" | "advanced" | "partial";

export interface VerifyTally {
  /** Edited rows across the entire log (from the chain walk). */
  editedTotal: number;
  /** Visible rows whose inclusion proof did not reconcile (internal inconsistency). */
  proofFailed: number;
  /** Visible rows proved against a root that drifted mid-pass (benign concurrent append). */
  advanced: number;
  /** Visible rows proven intact + included. */
  proven: number;
  /** Visible rows attempted in this pass. */
  attempted: number;
}

/**
 * Resolve the settled verdict for a completed verify pass.
 *
 * Precedence (a genuine problem always wins over a benign or partial one):
 *   broken   — any edited row ANYWHERE in the log, or any proof failure.
 *   advanced — none of the above, but the log grew mid-pass.
 *   partial  — clean so far, but some visible rows could not be proved
 *              (e.g. the proof route 404'd a compacted row).
 *   done     — every visible row proven and zero edited rows anywhere.
 */
export function decideVerifyPhase(tally: VerifyTally): VerifyPhase {
  if (tally.editedTotal > 0 || tally.proofFailed > 0) return "broken";
  if (tally.advanced > 0) return "advanced";
  if (tally.proven < tally.attempted) return "partial";
  return "done";
}
