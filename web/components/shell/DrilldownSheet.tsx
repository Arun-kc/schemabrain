"use client";

import { Suspense, useCallback, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Eyebrow, IconButton } from "@/components/kit";
import { cn } from "@/lib/cn";
import { useFocusTrap } from "@/lib/useFocusTrap";

/**
 * App-level entity drilldown sheet — the CONTAINER only.
 *
 * Addressed by `?entity=<name>` (ADR 0005 §3): readable from the graph,
 * the Entities index, or a PII row, and shareable/deep-linkable. The
 * body is a slot — the structured physical/semantic panes land with the
 * entity-drilldown work in a later PR; this one ships the mount/slide/
 * scrim/focus-trap/return-focus mechanics.
 *
 * Always mounted so it can slide; `inert` while closed keeps the
 * off-screen sheet out of the tab order + a11y tree. Closing replaces
 * the URL (drops ?entity= without a new history entry) — an open via
 * push() means Back also closes.
 */
function DrilldownSheetInner() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const ref = useRef<HTMLElement>(null);

  const entity = params.get("entity");
  const open = Boolean(entity);

  const close = useCallback(() => {
    router.replace(pathname);
  }, [router, pathname]);

  useFocusTrap(open, ref, close);

  return (
    <>
      <div
        className={cn("sb-drill-scrim", open && "show")}
        aria-hidden="true"
        onClick={close}
      />
      <aside
        ref={ref}
        className={cn("sb-drill", open && "show")}
        role="dialog"
        aria-modal="true"
        aria-label={entity ? `Entity ${entity}` : "Entity detail"}
        inert={!open}
      >
        <div className="sb-drill-head">
          <Eyebrow>Entity</Eyebrow>
          <h2>{entity ?? ""}</h2>
          <IconButton
            className="sb-drill-close"
            icon="x"
            label="Close entity detail"
            onClick={close}
          />
        </div>
        <div className="sb-drill-body">
          <p className="sb-drill-slot">
            Physical and semantic detail for this entity mounts here.
          </p>
        </div>
      </aside>
    </>
  );
}

export function DrilldownSheet() {
  // useSearchParams requires a Suspense boundary under static export.
  return (
    <Suspense fallback={null}>
      <DrilldownSheetInner />
    </Suspense>
  );
}
