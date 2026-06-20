import type { ReactNode } from "react";
import { SiteNav } from "@/components/SiteNav";
import { SiteFooter } from "@/components/SiteFooter";
import "./blog.css";

/**
 * Shared chrome for every /blog route. The `.sb-app` wrapper scopes the design
 * tokens (sb-theme.css) so all `var(--…)` resolve; `.ld` carries the landing
 * base. Nav + footer are shared so the index and each article stay consistent.
 */
export default function BlogLayout({ children }: { children: ReactNode }) {
  return (
    <div className="sb-app ld">
      <SiteNav />
      {children}
      <SiteFooter />
    </div>
  );
}
