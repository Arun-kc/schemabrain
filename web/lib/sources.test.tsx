import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Meta } from "./types/meta";

vi.mock("./api", () => ({ api: { meta: vi.fn() } }));

import { api } from "./api";
import { useSourceStore } from "./sourceStore";
import { useSourceId } from "./useSourceId";
import { useSources } from "./useSources";

const META: Meta = {
  charter_version: "1.2",
  dashboard_schema_version: "1.0",
  fingerprint_version: "fp-v1",
  store_path: "/tmp/store.db",
  default_source_connection_id: "src_default",
  source_connection_ids: ["src_default", "src_b"],
  sources: [
    {
      source_id: "src_default",
      engine: "postgres",
      state: "indexed",
      last_indexed_at: 1717000000,
      tables: 3,
      entities: 2,
    },
    {
      source_id: "src_b",
      engine: null,
      state: "indexed",
      last_indexed_at: null,
      tables: 1,
      entities: 0,
    },
  ],
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  useSourceStore.setState({ selectedSourceId: null });
  vi.mocked(api.meta).mockReset();
});

describe("useSources", () => {
  it("returns the source list and resolves the active to the default", async () => {
    vi.mocked(api.meta).mockResolvedValue(META);
    const { result } = renderHook(() => useSources(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.sources).toHaveLength(2);
    expect(result.current.defaultSourceId).toBe("src_default");
    expect(result.current.activeSourceId).toBe("src_default");
  });

  it("reflects an explicit selection as the active source", async () => {
    vi.mocked(api.meta).mockResolvedValue(META);
    useSourceStore.setState({ selectedSourceId: "src_b" });
    const { result } = renderHook(() => useSources(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.activeSourceId).toBe("src_b");
  });

  it("degrades to an empty list on error", async () => {
    vi.mocked(api.meta).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useSources(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.sources).toEqual([]);
    expect(result.current.defaultSourceId).toBeNull();
  });
});

describe("useSourceId", () => {
  it("resolves the meta default when nothing is selected", async () => {
    vi.mocked(api.meta).mockResolvedValue(META);
    const { result } = renderHook(() => useSourceId(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.sourceId).toBe("src_default");
  });

  it("prefers an explicit selection over the default", async () => {
    vi.mocked(api.meta).mockResolvedValue(META);
    useSourceStore.setState({ selectedSourceId: "src_b" });
    const { result } = renderHook(() => useSourceId(), { wrapper });

    // A selection is authoritative — resolves ready without waiting on meta.
    expect(result.current).toEqual({ sourceId: "src_b", status: "ready" });
  });

  it("surfaces the loading state before meta resolves", () => {
    vi.mocked(api.meta).mockReturnValue(new Promise<Meta>(() => {}));
    const { result } = renderHook(() => useSourceId(), { wrapper });
    expect(result.current).toEqual({ sourceId: null, status: "loading" });
  });

  it("surfaces the error state when meta fails", async () => {
    vi.mocked(api.meta).mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useSourceId(), { wrapper });

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.sourceId).toBeNull();
  });
});
