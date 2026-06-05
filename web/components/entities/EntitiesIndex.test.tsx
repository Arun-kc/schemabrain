import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EntityListItem, EntityListResponse, Meta } from "@/lib/types/meta";

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: vi.fn() }),
  usePathname: () => "/entities",
}));

vi.mock("@/lib/api", () => ({ api: { meta: vi.fn(), entities: vi.fn() } }));

import { api } from "@/lib/api";
import { useSourceStore } from "@/lib/sourceStore";
import { EntitiesIndex } from "./EntitiesIndex";

const META: Meta = {
  charter_version: "1.2",
  dashboard_schema_version: "1.8",
  fingerprint_version: "fp-v1",
  store_path: "/tmp/store.db",
  default_source_connection_id: "src_default",
  source_connection_ids: ["src_default"],
  sources: [],
};

function item(over: Partial<EntityListItem>): EntityListItem {
  return {
    name: "user",
    description: "",
    qualified_table: "public.users",
    identity: "id",
    group: "identity",
    origin: "suggested",
    inference_method: "llm_suggested",
    validation_state: "applied",
    rows: 1000,
    confidence: "high",
    pii_level: "none",
    metrics_count: 0,
    joins_count: 0,
    ...over,
  };
}

const RESPONSE: EntityListResponse = {
  source_connection_id: "src_default",
  count: 3,
  items: [
    item({ name: "user", group: "identity", rows: 1_200_000, confidence: "high", pii_level: "catastrophic", metrics_count: 2, joins_count: 3 }),
    item({ name: "plan", group: "billing", rows: 42, confidence: null, pii_level: "none", metrics_count: 0, joins_count: 1 }),
    item({ name: "session", group: "activity", rows: 94_000_000, confidence: "low", pii_level: "internal", metrics_count: 1, joins_count: 2 }),
  ],
  summary: { entities: 3, metrics: 3, joins: 6, catastrophic_entities: 1 },
};

function renderIndex() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EntitiesIndex />
    </QueryClientProvider>,
  );
}

function rowOrder(): string[] {
  // Drop the header row; each data row's only button is the entity-name button.
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((r) => within(r).getByRole("button").textContent ?? "");
}

beforeEach(() => {
  vi.mocked(api.meta).mockReset().mockResolvedValue(META);
  vi.mocked(api.entities).mockReset();
  mockPush.mockReset();
  useSourceStore.setState({ selectedSourceId: null });
});

describe("EntitiesIndex", () => {
  it("renders the hero, stat strip, and a row per entity", async () => {
    vi.mocked(api.entities).mockResolvedValue(RESPONSE);
    renderIndex();

    // "bound entities" only renders in the success state (the heading "Entities"
    // also shows while loading), so it gates the success assertions.
    expect(await screen.findByText("bound entities")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Entities" })).toBeInTheDocument();
    expect(screen.getByText("1 on the catastrophic floor")).toBeInTheDocument();
    expect(screen.getByText("catastrophic PII")).toBeInTheDocument();
    // 3 data rows.
    expect(rowOrder()).toHaveLength(3);
    // Confidence + PII chips render; the hand-authored row shows an em-dash.
    expect(screen.getByText("Catastrophic")).toBeInTheDocument();
    expect(screen.getByText("1.2M")).toBeInTheDocument();
  });

  it("defaults to rows-descending and re-sorts when a header is clicked", async () => {
    vi.mocked(api.entities).mockResolvedValue(RESPONSE);
    renderIndex();
    await screen.findByRole("table");

    // Default: largest table first.
    expect(rowOrder()).toEqual(["session", "user", "plan"]);
    expect(screen.getByRole("columnheader", { name: /Rows/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );

    // Sort by Entity → descending name first (u > s > p).
    fireEvent.click(screen.getByRole("button", { name: "Entity" }));
    expect(screen.getByRole("columnheader", { name: /Entity/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    expect(rowOrder()).toEqual(["user", "session", "plan"]);

    // Re-click toggles to ascending.
    fireEvent.click(screen.getByRole("button", { name: "Entity" }));
    expect(screen.getByRole("columnheader", { name: /Entity/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    expect(rowOrder()).toEqual(["plan", "session", "user"]);
  });

  it("opens the drilldown sheet via ?entity= when a row is selected", async () => {
    vi.mocked(api.entities).mockResolvedValue(RESPONSE);
    renderIndex();
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: "user" }));
    expect(mockPush).toHaveBeenCalledWith("/entities?entity=user");
  });

  it("shows an empty state when the source has no bound entities", async () => {
    vi.mocked(api.entities).mockResolvedValue({
      source_connection_id: "src_default",
      count: 0,
      items: [],
      summary: { entities: 0, metrics: 0, joins: 0, catastrophic_entities: 0 },
    });
    renderIndex();

    expect(await screen.findByText("No entities bound yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders the no-source error with init/index guidance", async () => {
    vi.mocked(api.entities).mockRejectedValue(
      new Error("sidecar /api/entities returned 409: store has no indexed sources"),
    );
    renderIndex();

    expect(await screen.findByText("Point SchemaBrain at a database.")).toBeInTheDocument();
    expect(screen.getByText(/schemabrain init/)).toBeInTheDocument();
  });

  it("renders a generic error when the query fails for another reason", async () => {
    vi.mocked(api.entities).mockRejectedValue(new Error("boom"));
    renderIndex();

    expect(await screen.findByText("Couldn’t load the entities.")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("shows a loading state before the fetch resolves", async () => {
    vi.mocked(api.entities).mockReturnValue(new Promise(() => {}));
    renderIndex();

    expect(screen.getByText(/loading entities/)).toBeInTheDocument();
    await waitFor(() => expect(api.entities).toHaveBeenCalled());
  });
});
