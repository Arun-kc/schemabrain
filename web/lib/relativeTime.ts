/**
 * Human "time ago" for an epoch-seconds timestamp.
 *
 * Honest by design: the caller passes a real timestamp (the sidecar never
 * fabricates one) and renders "—" itself for the null case — this helper
 * only formats a known instant. `nowMs` is injectable so the formatting is
 * deterministic under test.
 */
export function formatRelativeTime(epochSeconds: number, nowMs: number = Date.now()): string {
  const deltaSeconds = Math.floor(nowMs / 1000 - epochSeconds);
  // Clock skew (future timestamp) — never render a negative age.
  if (deltaSeconds < 45) return "just now";
  const minutes = Math.floor(deltaSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}
