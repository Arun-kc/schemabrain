"use client";

import Link from "next/link";
import { BrainMark, Icon, IconButton } from "@/components/kit";
import { useTheme } from "@/lib/useTheme";
import { SourceSelector } from "./SourceSelector";

interface TopBarProps {
  /** Open the command palette (the ⌘K affordance). */
  onSearch: () => void;
}

/**
 * Shell top bar (handoff app/shell.jsx `TopBar`): brand → source
 * selector → search/⌘K trigger → theme toggle → account.
 *
 * The theme toggle drives the shared `useTheme` hook (no key={theme}
 * remount; repaints in place), so the whole shell + its surfaces flip
 * theme without losing scroll/focus/in-flight state.
 */
export function TopBar({ onSearch }: TopBarProps) {
  const { theme, toggleTheme } = useTheme();
  const toDark = theme !== "dark";

  return (
    <header className="sb-topbar">
      {/* Brand returns to the marketing landing (outside the app shell). */}
      <Link href="/" className="sb-brand" aria-label="schemabrain home">
        <BrainMark size={24} />
        <span className="wm">schemabrain</span>
      </Link>
      <SourceSelector />
      <div className="sb-top-right">
        <button
          type="button"
          className="sb-search"
          onClick={onSearch}
          aria-label="Open command palette"
        >
          <Icon name="search" size={14} />
          Search…
          <span className="kbd" aria-hidden="true">
            ⌘K
          </span>
        </button>
        <IconButton
          icon={toDark ? "moon" : "sun"}
          label={toDark ? "Switch to dark theme" : "Switch to light theme"}
          onClick={toggleTheme}
        />
        <IconButton icon="user" label="Account" />
      </div>
    </header>
  );
}
