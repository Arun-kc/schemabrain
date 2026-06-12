import { Overview } from "@/components/overview/Overview";

/**
 * Overview surface (/overview) — the dashboard home, mounted inside the app
 * shell (the shell owns the chrome — top bar, nav rail, source selector,
 * theme). Read-only system-status summary: a protected-graph hero plus a
 * 6-card bento where every card opens its detail surface.
 */
export default function OverviewPage() {
  return <Overview />;
}
