// Typed fetch helpers for the dashboard sidecar's read-only routes.
// All helpers throw on non-2xx so TanStack Query's error boundary
// can surface the failure with the route + status in dev tools.

import type {
  AuditChainStatus,
  DriftResponse,
  EntityDrilldownResponse,
  EntityListResponse,
  HealthResponse,
  Meta,
  PaginatedAuditResponse,
  PiiMatrixResponse,
  PolicyPreviewResponse,
  PolicyResponse,
  RefusalEntry,
} from "@/lib/types";

class SidecarError extends Error {
  constructor(
    readonly status: number,
    readonly route: string,
    readonly detail: string,
  ) {
    super(`sidecar ${route} returned ${status}: ${detail}`);
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // Fall through to statusText
    }
    throw new SidecarError(response.status, path, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  meta: () => getJson<Meta>("/api/meta"),
  health: () => getJson<HealthResponse>("/api/health"),

  entities: (sourceId?: string) => {
    const qs = sourceId ? `?source_connection_id=${encodeURIComponent(sourceId)}` : "";
    return getJson<EntityListResponse>(`/api/entities${qs}`);
  },

  entityColumns: (name: string, sourceId?: string) => {
    const qs = sourceId ? `?source_connection_id=${encodeURIComponent(sourceId)}` : "";
    return getJson<EntityDrilldownResponse>(
      `/api/entities/${encodeURIComponent(name)}/columns${qs}`,
    );
  },

  piiMatrix: (sourceId?: string) => {
    const qs = sourceId ? `?source_connection_id=${encodeURIComponent(sourceId)}` : "";
    return getJson<PiiMatrixResponse>(`/api/entities/pii-matrix${qs}`);
  },

  piiPolicy: (sourceId?: string) => {
    const qs = sourceId ? `?source_connection_id=${encodeURIComponent(sourceId)}` : "";
    return getJson<PolicyResponse>(`/api/pii/policy${qs}`);
  },

  drift: (sourceId?: string) => {
    const qs = sourceId ? `?source_connection_id=${encodeURIComponent(sourceId)}` : "";
    return getJson<DriftResponse>(`/api/drift${qs}`);
  },

  /**
   * Render the canonical YAML + diff for a staged policy (ADR 0006).
   * Source-independent (qualified columns are global), so no source id.
   * `block` / `override` are repeatable query params — see lib/policy.ts
   * for the override wire encoding.
   */
  piiPolicyPreview: (params: { block: readonly string[]; override: readonly string[] }) => {
    const qs = new URLSearchParams();
    for (const category of params.block) qs.append("block", category);
    for (const override of params.override) qs.append("override", override);
    const tail = qs.toString() ? `?${qs.toString()}` : "";
    return getJson<PolicyPreviewResponse>(`/api/pii/policy/preview${tail}`);
  },

  auditRows: (params?: { limit?: number; offset?: number; status?: string }) => {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.offset) qs.set("offset", String(params.offset));
    if (params?.status) qs.set("status", params.status);
    const tail = qs.toString() ? `?${qs}` : "";
    return getJson<PaginatedAuditResponse<unknown>>(`/api/audit/rows${tail}`);
  },

  auditRefusals: (params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.offset) qs.set("offset", String(params.offset));
    const tail = qs.toString() ? `?${qs}` : "";
    return getJson<PaginatedAuditResponse<RefusalEntry>>(`/api/audit/refusals${tail}`);
  },

  auditRefusalDetail: (id: number) =>
    getJson<RefusalEntry>(`/api/audit/refusals/${id}`),

  auditVerify: (since?: string) => {
    const qs = since ? `?since=${encodeURIComponent(since)}` : "";
    return getJson<AuditChainStatus>(`/api/audit/verify${qs}`);
  },
};

export { SidecarError };
