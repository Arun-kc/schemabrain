"use client";

import Link from "next/link";
import { cn } from "@/lib/cn";
import { useSourceId } from "@/lib/useSourceId";
import { ThemeToggle } from "./ThemeToggle";
import styles from "./header.module.css";

/**
 * Full-bleed dashboard header strip used by every surface (PII
 * Ledger, Refusal UI, Audit Viewer). Composition:
 *
 *   [LOGO] SCHEMABRAIN · dashboard / <surface>           ● <source>   [light │ dark]
 *
 * The 1px ink-700 underline (heavier than a generic hairline)
 * anchors the surface below it. Source status uses signal-green dot
 * when connected, amber while resolving, red when no source.
 *
 * The source connection id is resolved from /api/meta via the
 * shared useSourceId() hook — never passed in as a prop. This keeps
 * pages free of hardcoded source IDs that would break for any
 * operator whose store has a different source hash.
 */

type SourceStatus = "connected" | "degraded" | "absent";

interface HeaderStripProps {
  surface: string;
  className?: string;
}

const STATUS_VAR: Record<SourceStatus, string> = {
  connected: "var(--color-signal-green)",
  degraded: "var(--color-signal-amber)",
  absent: "var(--color-signal-red)",
};

export function HeaderStrip({ surface, className }: HeaderStripProps) {
  const { sourceId, status: resolveStatus } = useSourceId();
  const displaySource =
    resolveStatus === "loading"
      ? "resolving…"
      : (sourceId ?? "no source connected");
  const status: SourceStatus =
    resolveStatus === "loading"
      ? "degraded"
      : sourceId
        ? "connected"
        : "absent";

  return (
    <header className={cn(styles.strip, className)}>
      <div className={styles.inner}>
        <Link href="/" className={cn(styles.brandGroup, "group flex items-center gap-3 select-none hover:opacity-90 transition-opacity")}>
          <div className="flex items-center gap-2">
            <MiniLogo />
            <span className={styles.brand}>schemabrain</span>
          </div>
          <span className={styles.separator} aria-hidden="true">
            ·
          </span>
          <span className={styles.crumb} aria-label={`dashboard breadcrumb: ${surface}`}>
            dashboard / <span className={styles.crumbActive}>{surface}</span>
          </span>
        </Link>
        <div className={styles.rightGroup}>
          <span className={styles.sourcePill}>
            <span
              aria-hidden="true"
              className={styles.statusDot}
              style={{ backgroundColor: STATUS_VAR[status] }}
            />
            <span aria-label={`source connection: ${displaySource}`}>{displaySource}</span>
          </span>
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

/* ─────────── BRAND-ALIGNED MINI SVG LOGO ─────────── */

function MiniLogo() {
  return (
    <svg width="22" height="22" viewBox="0 0 64 64" overflow="visible" className="shrink-0">
      <defs>
        <clipPath id="clip-logo-mini">
          <path d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z"></path>
        </clipPath>
      </defs>
      
      {/* Glowing aura under the logo */}
      <path 
        d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z" 
        fill="#3ECF8E" 
        opacity="0.2" 
      />

      {/* Half Green Fill */}
      <g clipPath="url(#clip-logo-mini)">
        <rect x="32" y="0" width="32" height="64" fill="#3ECF8E" />
      </g>

      {/* Logo Outlines */}
      <path 
        d="M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z" 
        stroke="var(--text-primary)" 
        strokeWidth="2.4" 
        strokeLinejoin="round" 
        fill="none"
      />
      <line x1="32" y1="10" x2="32" y2="52" stroke="var(--text-primary)" strokeWidth="1.8" strokeLinecap="round" />
      
      {/* Wavy lines on left (database rows) */}
      <path d="M14 22 C 20 19, 26 22, 30 21" stroke="var(--text-primary)" strokeWidth="1.6" strokeLinecap="round" fill="none" />
      <path d="M12 32 C 18 28, 26 33, 30 31" stroke="var(--text-primary)" strokeWidth="1.6" strokeLinecap="round" fill="none" />
      <path d="M14 42 C 20 39, 26 43, 30 41" stroke="var(--text-primary)" strokeWidth="1.6" strokeLinecap="round" fill="none" />
      
      {/* Dots on green side (AI nodes) */}
      <g clipPath="url(#clip-logo-mini)">
        <circle cx="36" cy="22" r="1.5" fill="var(--surface-base)" />
        <line x1="38.5" y1="22" x2="50" y2="22" stroke="var(--surface-base)" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="36" cy="32" r="1.5" fill="var(--surface-base)" />
        <line x1="38.5" y1="32" x2="52" y2="32" stroke="var(--surface-base)" strokeWidth="1.8" strokeLinecap="round" />
        <circle cx="36" cy="42" r="1.5" fill="var(--surface-base)" />
        <line x1="38.5" y1="42" x2="50" y2="42" stroke="var(--surface-base)" strokeWidth="1.8" strokeLinecap="round" />
      </g>
    </svg>
  );
}

