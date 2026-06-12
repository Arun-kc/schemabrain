import type { IconName } from "@/components/kit";

/**
 * Shell navigation model (handoff app/shell.jsx `NAV`).
 *
 * Nine app surfaces across four groups (Home / Explore / Trust / Health).
 * `built` gates whether the item is a live link in THIS release — it renders
 * disabled (dimmed, non-navigable, no dead 404 link) until its route exists.
 * Each surface PR flips its own item's `built` to true. The marketing landing
 * (`/`) is intentionally NOT in this rail — it has its own top nav; the
 * dashboard's own home is Overview (`/overview`).
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
    title: "Home",
    items: [
      { id: "overview", href: "/overview", label: "Overview", icon: "layout-dashboard", built: true },
    ],
  },
  {
    title: "Explore",
    items: [
      { id: "graph", href: "/graph", label: "Knowledge graph", icon: "waypoints", built: true },
      { id: "entities", href: "/entities", label: "Entities", icon: "boxes", built: true },
      { id: "dict", href: "/dict", label: "Data dictionary", icon: "book-open", built: true },
    ],
  },
  {
    title: "Trust",
    items: [
      { id: "pii", href: "/pii", label: "PII matrix", icon: "shield", built: true },
      { id: "refusals", href: "/refusals", label: "Refusals", icon: "ban", built: true },
      { id: "audit", href: "/audit", label: "Audit", icon: "scroll-text", built: true },
      { id: "policy", href: "/policy", label: "Policy", icon: "scale", built: true },
    ],
  },
  {
    title: "Health",
    items: [{ id: "drift", href: "/drift", label: "Drift", icon: "radar", built: true }],
  },
];

/** True when `pathname` is (or is under) the item's route. */
export function isNavItemActive(item: NavItem, pathname: string): boolean {
  return pathname === item.href || pathname.startsWith(`${item.href}/`);
}
