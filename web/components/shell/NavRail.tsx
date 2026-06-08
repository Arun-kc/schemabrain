"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Icon } from "@/components/kit";
import { cn } from "@/lib/cn";
import { useSources } from "@/lib/useSources";
import { NAV, isNavItemActive } from "./nav";

/**
 * Shell navigation rail (handoff app/shell.jsx `NavRail`).
 *
 * Renders all nine surfaces for layout fidelity. Built surfaces are
 * live `next/link`s (soft navigation keeps the shell mounted — ADR
 * 0005); not-yet-built surfaces render disabled (dimmed, non-focusable,
 * no dead 404 link, no fabricated badge). The active link carries
 * `aria-current="page"` and the green active treatment.
 *
 * The footer reflects the active source's real index state (or nothing,
 * when no source is resolved) — never a fabricated "2m ago".
 */
export function NavRail() {
  const pathname = usePathname();
  const { sources, activeSourceId } = useSources();
  const active = sources.find((s) => s.source_id === activeSourceId);

  return (
    <nav className="sb-nav" aria-label="Dashboard sections">
      {NAV.map((group) => (
        <div key={group.title} role="group" aria-label={group.title}>
          <div className="sb-nav-group">{group.title}</div>
          {group.items.map((item) => {
            if (!item.built) {
              return (
                <span
                  key={item.id}
                  className="sb-nav-item disabled"
                  aria-disabled="true"
                  title={`${item.label} — coming soon`}
                >
                  <Icon name={item.icon} size={17} />
                  {item.label}
                  <span className="soon">soon</span>
                </span>
              );
            }
            const isActive = isNavItemActive(item, pathname);
            return (
              <Link
                key={item.id}
                href={item.href}
                className={cn("sb-nav-item", isActive && "active")}
                aria-current={isActive ? "page" : undefined}
              >
                <Icon name={item.icon} size={17} />
                {item.label}
              </Link>
            );
          })}
        </div>
      ))}
      {active && (
        <div className="sb-nav-foot">
          <span className={cn("sb-statedot", active.state)} aria-hidden="true" />
          {active.state}
        </div>
      )}
    </nav>
  );
}
