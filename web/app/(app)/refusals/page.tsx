import { Timeline } from "@/components/refusals/Timeline";

/**
 * Refusals surface (/refusals), mounted inside the app shell (the shell owns
 * the chrome: top bar, nav rail, theme — see (app)/layout.tsx).
 *
 * THE LEDGER — a single protective-framed timeline of blocked attempts (the
 * instrument sibling of the /pii heatmap). Read-only; refusals are global, so
 * this surface is not source-scoped.
 */
export default function RefusalsPage() {
  return <Timeline />;
}
