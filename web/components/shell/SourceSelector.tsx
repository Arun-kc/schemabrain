"use client";

import { useState } from "react";
import { Icon } from "@/components/kit";
import { cn } from "@/lib/cn";
import { useSourceStore } from "@/lib/sourceStore";
import { useSources } from "@/lib/useSources";
import type { SourceInfo } from "@/lib/types/meta";

/** Secondary line: dialect (when known) · table count. */
function describeSource(source: SourceInfo): string {
  const tableLabel = `${source.tables} ${source.tables === 1 ? "table" : "tables"}`;
  return source.engine ? `${source.engine} · ${tableLabel}` : tableLabel;
}

/**
 * Top-bar source selector (handoff app/shell.jsx `SourceSelector`).
 *
 * Reflects the resolved active source and, on pick, writes the
 * selection to the shared source store — which `useSourceId` honours,
 * so the active source genuinely drives source-scoped surfaces. Hidden
 * dialect (engine=null) and zero-source stores degrade honestly: the
 * dialect line is omitted and the trigger disables rather than faking a
 * source.
 */
export function SourceSelector() {
  const { sources, activeSourceId, status } = useSources();
  const setSelectedSource = useSourceStore((s) => s.setSelectedSource);
  const [open, setOpen] = useState(false);

  const active = sources.find((s) => s.source_id === activeSourceId);
  const label = active
    ? active.source_id
    : status === "loading"
      ? "resolving…"
      : "no source";
  const state = active?.state ?? "empty";
  const hasSources = sources.length > 0;

  return (
    <div className="sb-src">
      <button
        type="button"
        className="sb-src-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`active source: ${label}`}
        disabled={!hasSources}
        onClick={() => setOpen((o) => !o)}
      >
        <Icon name="database" size={15} />
        <span className="lbl">{label}</span>
        <span className={cn("sb-statedot", state)} aria-hidden="true" />
        <Icon name="chevron-down" size={14} />
      </button>
      {open && hasSources && (
        <>
          <div
            className="sb-src-backdrop"
            aria-hidden="true"
            onClick={() => setOpen(false)}
          />
          <div className="sb-src-menu" role="menu" aria-label="Select source">
            {sources.map((source) => (
              <button
                key={source.source_id}
                type="button"
                role="menuitemradio"
                aria-checked={source.source_id === activeSourceId}
                data-active={source.source_id === activeSourceId}
                className="sb-src-item"
                onClick={() => {
                  setSelectedSource(source.source_id);
                  setOpen(false);
                }}
              >
                <Icon name="database" size={16} />
                <span className="meta">
                  <span className="t">{source.source_id}</span>
                  <span className="s">{describeSource(source)}</span>
                </span>
                <span className={cn("sb-statedot", source.state)} aria-hidden="true" />
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
