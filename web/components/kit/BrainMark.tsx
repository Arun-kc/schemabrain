"use client";

import { useId } from "react";

interface BrainMarkProps {
  size?: number;
  className?: string;
  /** Optional accessible name; omit to render the mark as decorative. */
  title?: string;
}

/**
 * The single canonical SchemaBrain mark.
 *
 * Replaces the two duplicated inline SVGs in the old HeaderStrip and landing
 * page. Token-driven (var(--green) / var(--ink)) so it repaints with the
 * theme — no JS theme prop, no hardcoded hex. The clip-path id is uniquified
 * with useId so multiple marks can coexist without colliding.
 */
const OUTLINE =
  "M22 12 C 22 8, 30 6, 32 10 C 36 6, 44 8, 44 14 C 50 12, 56 18, 52 24 C 58 28, 56 36, 50 38 C 54 44, 48 52, 40 50 C 38 56, 28 56, 26 52 C 18 54, 12 48, 14 42 C 8 40, 6 32, 12 30 C 8 24, 12 16, 18 18 C 18 14, 20 12, 22 12 Z";

export function BrainMark({ size = 24, className, title }: BrainMarkProps) {
  const clip = useId();
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      className={className}
      style={{ display: "block" }}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <defs>
        <clipPath id={clip}>
          <path d={OUTLINE} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${clip})`}>
        <rect x="0" y="0" width="64" height="64" fill="transparent" />
        <rect x="32" y="0" width="32" height="64" fill="var(--green)" />
      </g>
      <path d={OUTLINE} stroke="var(--ink)" strokeWidth="2.6" strokeLinejoin="round" fill="none" />
      <line x1="32" y1="10" x2="32" y2="52" stroke="var(--ink)" strokeWidth="2" strokeLinecap="round" />
      <path d="M14 22 C 20 19, 26 22, 30 21" stroke="var(--ink)" strokeWidth="1.8" strokeLinecap="round" fill="none" />
      <path d="M12 32 C 18 28, 26 33, 30 31" stroke="var(--ink)" strokeWidth="1.8" strokeLinecap="round" fill="none" />
      <path d="M14 42 C 20 39, 26 43, 30 41" stroke="var(--ink)" strokeWidth="1.8" strokeLinecap="round" fill="none" />
      <g clipPath={`url(#${clip})`}>
        <circle cx="36" cy="22" r="1.8" fill="var(--ink)" />
        <line x1="39" y1="22" x2="50" y2="22" stroke="var(--ink)" strokeWidth="2" strokeLinecap="round" />
        <circle cx="36" cy="32" r="1.8" fill="var(--ink)" />
        <line x1="39" y1="32" x2="52" y2="32" stroke="var(--ink)" strokeWidth="2" strokeLinecap="round" />
        <circle cx="36" cy="42" r="1.8" fill="var(--ink)" />
        <line x1="39" y1="42" x2="50" y2="42" stroke="var(--ink)" strokeWidth="2" strokeLinecap="round" />
      </g>
    </svg>
  );
}
