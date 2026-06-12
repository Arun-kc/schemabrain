"use client";

import { Suspense, useCallback, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { cn } from "@/lib/cn";
import { useFocusTrap } from "@/lib/useFocusTrap";
import { EntityDrilldownBody } from "@/components/entities/EntityDrilldownBody";

/**
 * App-level entity drilldown sheet — the CONTAINER.
 *
 * Addressed by `?entity=<name>` (ADR 0005 §3): readable from the graph,
 * the Entities index, or a PII row, and shareable/deep-linkable. The
 * structured header + physical/semantic panes are the wsENT body
 * (`EntityDrilldownBody`), rendered into this container's slide-over; the
 * container owns only the mount/slide/scrim/focus-trap/return-focus
 * mechanics, per the critic's "shell owns container, wsENT owns body" seam.
 *
 * Always mounted so it can slide; `inert` while closed keeps the
 * off-screen sheet out of the tab order + a11y tree. The body mounts only
 * while open (it fetches lazily). Closing replaces the URL (drops ?entity=
 * without a new history entry) — an open via push() means Back also closes.
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
        {entity && <EntityDrilldownBody entity={entity} onClose={close} />}
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
