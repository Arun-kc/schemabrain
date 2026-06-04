import { Drift } from "@/components/drift/Drift";

/**
 * Drift surface (/drift), mounted inside the app shell (the shell owns the
 * chrome — top bar, nav rail, source selector, theme). Read-only context-
 * health view: config drift (policy vs serve) + enrichment drift (prompt-
 * version staleness) + a freshness roll-up.
 */
export default function DriftPage() {
  return <Drift />;
}
