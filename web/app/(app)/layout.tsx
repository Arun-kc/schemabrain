import type { ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";

/**
 * Layout for every app surface (graph, entities, dict, pii, refusals,
 * audit, policy, drift). The shared shell lives here so App-Router soft
 * navigation keeps it mounted across these sibling routes (ADR 0005).
 * The marketing landing (`/`) sits OUTSIDE this group and is unaffected.
 *
 * Route groups are path-neutral — `(app)/pii` still exports `pii.html`,
 * so the static-export + sidecar serving contract is unchanged.
 */
export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
