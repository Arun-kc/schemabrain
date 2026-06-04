import { AuditLedger } from "@/components/audit/AuditLedger";

/**
 * Audit surface (/audit), mounted inside the app shell (the shell owns the
 * chrome: top bar, nav rail, theme — see (app)/layout.tsx).
 *
 * THE PROOF — an append-only run of audit rows spined by the derived Merkle
 * root, with a real per-row inclusion-proof check that reconciles each row to
 * one global root (the instrument sibling of /pii and /refusals). Read-only;
 * audit rows are global, so this surface is not source-scoped.
 */
export default function AuditPage() {
  return <AuditLedger />;
}
