import type { IconName } from "@/components/kit";

/**
 * Shell navigation model (handoff app/shell.jsx `NAV`).
 *
 * Eight app surfaces across three groups. `built` gates whether the item
 * is a live link in THIS release — graph/entities/dict/policy/drift land
 * in later PRs, so they render disabled (dimmed, non-navigable, no dead
 * 404 link) until their route exists. Each surface PR flips its own
 * item's `built` to true. The marketing landing (`/`) is intentionally
 * NOT in this rail — it has its own top nav.
 */
export interface NavItem {
  /** Route key, also the active-match segment (e.g. "pii" ⇒ /pii). */
  id: string;
  href: string;
  label: string;
  icon: IconName;
  /** False until the surface's own PR ships it; renders disabled. */
  built: boolean;
}

export interface NavGroup {
  title: string;
  items: readonly NavItem[];
}

export const NAV: readonly NavGroup[] = [
  {
    title: "Explore",
    items: [
      { id: "graph", href: "/graph", label: "Knowledge graph", icon: "waypoints", built: false },
      { id: "entities", href: "/entities", label: "Entities", icon: "boxes", built: false },
      { id: "dict", href: "/dict", label: "Data dictionary", icon: "book-open", built: false },
    ],
  },
  {
    title: "Trust",
    items: [
      { id: "pii", href: "/pii", label: "PII matrix", icon: "shield", built: true },
      { id: "refusals", href: "/refusals", label: "Refusals", icon: "ban", built: true },
      { id: "audit", href: "/audit", label: "Audit", icon: "scroll-text", built: true },
      { id: "policy", href: "/policy", label: "Policy", icon: "scale", built: false },
    ],
  },
  {
    title: "Health",
    items: [{ id: "drift", href: "/drift", label: "Drift", icon: "radar", built: false }],
  },
];

/** True when `pathname` is (or is under) the item's route. */
export function isNavItemActive(item: NavItem, pathname: string): boolean {
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}
