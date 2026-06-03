import { Matrix } from "@/components/pii/Matrix";

/**
 * PII matrix surface (/pii), mounted inside the app shell (the shell owns
 * the chrome: top bar, nav rail, source selector, theme — see
 * (app)/layout.tsx).
 *
 * The editable policy view moved to its own /policy route (ADR 0007), so
 * /pii is now the PII matrix alone. The Matrix body still uses the legacy
 * token palette; reskinning it onto the kit/tokens is a later PR.
 */
export default function PiiPage() {
  return <Matrix />;
}
