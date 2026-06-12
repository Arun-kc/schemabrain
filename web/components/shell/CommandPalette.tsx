"use client";

import { useRef } from "react";
import { Icon } from "@/components/kit";
import { useFocusTrap } from "@/lib/useFocusTrap";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Command-palette stub (the ⌘K affordance the shell promises).
 *
 * Open/close + focus-trap + Escape are real; the search itself is a
 * stub — full cross-surface command routing is a later surface (ADR
 * 0005 "remains deferred"). Mounts on open so the closed shell ships no
 * inert dialog; the focus trap returns focus to the trigger on close.
 */
export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(open, ref, onClose);

  if (!open) return null;

  return (
    <div className="sb-cmdk-scrim" role="presentation" onClick={onClose}>
      <div
        ref={ref}
        className="sb-cmdk"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sb-cmdk-input">
          <Icon name="search" size={16} />
          <input
            type="text"
            placeholder="Search surfaces and entities…"
            aria-label="Command palette search"
          />
        </div>
        <p className="sb-cmdk-note">
          Command palette is coming soon — full search across surfaces and
          entities lands with the knowledge graph. Press Esc to close.
        </p>
      </div>
    </div>
  );
}
