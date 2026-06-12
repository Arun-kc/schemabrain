"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type PolicyFilter, type PolicyStatusFilter } from "@/lib/policy";
import { PII_CATEGORIES, type PIICategory } from "@/lib/types";

/**
 * usePolicyFilter — bridges the per-column grid filter to URL search params
 * (`?q=`, `?cat=`, `?status=`) so a filtered view is shareable + deep-linkable
 * (the house URL-as-state pattern). Seeds once from the URL for instant deep
 * links, renders from local state for instant input, and writes the URL
 * debounced so rapid typing doesn't spam `router.replace`.
 *
 * MUST be called from a component inside a `<Suspense>` boundary —
 * `useSearchParams` requires one under `output: "export"` (same constraint as
 * DrilldownSheet). Back/forward navigation after mount is not re-synced into
 * local state (a filter is ephemeral); deep links on fresh load are.
 */
const QUERY_DEBOUNCE_MS = 250;
const STATUSES: readonly PolicyStatusFilter[] = ["block", "redact", "allow", "floor"];

interface ReadableParams {
  get(name: string): string | null;
}

function parseFilter(params: ReadableParams): PolicyFilter {
  const query = params.get("q") ?? "";
  const catRaw = params.get("cat");
  const category =
    catRaw && PII_CATEGORIES.includes(catRaw as PIICategory) ? (catRaw as PIICategory) : null;
  const statusRaw = params.get("status");
  const status =
    statusRaw && STATUSES.includes(statusRaw as PolicyStatusFilter)
      ? (statusRaw as PolicyStatusFilter)
      : null;
  return { query, category, status };
}

function serializeFilter(filter: PolicyFilter): string {
  const params = new URLSearchParams();
  const query = filter.query.trim();
  if (query) params.set("q", query);
  if (filter.category) params.set("cat", filter.category);
  if (filter.status) params.set("status", filter.status);
  return params.toString();
}

export function usePolicyFilter(): {
  filter: PolicyFilter;
  setFilter: (next: PolicyFilter) => void;
} {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const [filter, setLocal] = useState<PolicyFilter>(() => parseFilter(params));
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const writeUrl = useCallback(
    (next: PolicyFilter) => {
      const qs = serializeFilter(next);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );

  const setFilter = useCallback(
    (next: PolicyFilter) => {
      setLocal(next);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => writeUrl(next), QUERY_DEBOUNCE_MS);
    },
    [writeUrl],
  );

  // Cancel a pending debounced write on unmount AND whenever the target path
  // changes (writeUrl identity changes), so a queued write never lands on the
  // wrong route.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [writeUrl],
  );

  return { filter, setFilter };
}
