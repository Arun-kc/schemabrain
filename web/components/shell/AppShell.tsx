"use client";

import { useEffect, useState, type ReactNode } from "react";
import { ToastProvider } from "@/components/kit";
import { CommandPalette } from "./CommandPalette";
import { DrilldownSheet } from "./DrilldownSheet";
import { NavRail } from "./NavRail";
import { TopBar } from "./TopBar";

/**
 * The persistent dashboard app shell (handoff app/shell.jsx + app.jsx).
 *
 * Lives in the (app) route-group layout, so App-Router soft navigation
 * keeps it mounted across sibling routes — nav active state, source
 * selection, theme, scroll, and ⌘K survive route changes with no flash
 * (ADR 0005 §2). `.sb-app` scopes the dual-theme token system; the
 * `data-theme` attribute it keys off lives on <html> (set pre-hydration
 * by layout.tsx + owned by useTheme), so the toggle never remounts.
 *
 * Landmarks: <header> top bar, <nav> rail, <main> content. A skip link
 * jumps straight to the content. The drilldown sheet + toast are
 * app-level, rendered once here over every surface.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const [commandOpen, setCommandOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((open) => !open);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div className="sb-app">
      <ToastProvider>
        <a href="#sb-main" className="sb-skip">
          Skip to content
        </a>
        <div className="sb-shell">
          <TopBar onSearch={() => setCommandOpen(true)} />
          <NavRail />
          <main id="sb-main" className="sb-content">
            <div className="sb-content-scroll">{children}</div>
            <DrilldownSheet />
          </main>
        </div>
        <CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)} />
      </ToastProvider>
    </div>
  );
}
