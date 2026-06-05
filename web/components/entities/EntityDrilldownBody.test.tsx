import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EntityDrilldownResponse, Meta } from "@/lib/types/meta";

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({ api: { meta: vi.fn(), entityColumns: vi.fn() } }));

import { api } from "@/lib/api";
import { useSourceStore } from "@/lib/sourceStore";
import { ToastProvider } from "@/components/kit";
import { EntityDrilldownBody } from "./EntityDrilldownBody";

const META: Meta = {
  charter_version: "1.2",
  dashboard_schema_version: "1.8",
  fingerprint_version: "fp-v1",
  store_path: "/tmp/store.db",
  default_source_connection_id: "src_default",
  source_connection_ids: ["src_default"],
  sources: [],
};

function header(over: Partial<EntityDrilldownResponse["entity"]> = {}) {
  return {
    name: "user",
    description: "",
    qualified_table: "public.users",
    identity: "id",
    group: "identity" as const,
    origin: "suggested" as const,
    inference_method: "llm_suggested" as const,
    validation_state: "applied" as const,
    rows: 1_200_000,
    confidence: "high" as const,
    rationale: "Bound from primary-key + foreign-key evidence.",
    ...over,
  };
}

const FULL: EntityDrilldownResponse = {
  entity: header(),
  columns: [
    { name: "id", data_type: "uuid", description: "primary key", sensitivity: "public", pii_categories: [], similar: [] },
    {
      name: "email",
      data_type: "text",
      description: "contact email",
      sensitivity: "pii",
      pii_categories: ["contact"],
      similar: [
        { schema: "public", table: "invoice", column: "billing_email", score: 0.94 },
        { schema: "public", table: "lead", column: "work_email", score: 0.88 },
        { schema: "public", table: "a", column: "c1", score: 0.8 },
        { schema: "public", table: "a", column: "c2", score: 0.79 },
        { schema: "public", table: "a", column: "c3", score: 0.78 },
        { schema: "public", table: "a", column: "c4", score: 0.77 },
        { schema: "public", table: "a", column: "c5", score: 0.76 },
      ],
    },
    {
      name: "password_hash",
      data_type: "text",
      description: "argon2 hash",
      sensitivity: "pii",
      pii_categories: ["credential"],
      similar: [],
    },
  ],
  metrics: [
    {
      name: "active_users",
      description: "distinct active users",
      measure: { agg: "count_distinct", column: "id", expression: null },
      time_grains: ["day"],
    },
  ],
  joins: [
    {
      name: "subscription_user",
      description: "a subscription belongs to a user",
      source_entity: "subscription",
      target_entity: "user",
      on_clause: '"subscription"."user_id" = "user"."id"',
      cardinality: "many_to_one",
      origin: "suggested",
      inference_method: "fk_constraint",
      is_log_mined: false,
    },
    {
      name: "session_user",
      description: "log-mined session link",
      source_entity: "session",
      target_entity: "user",
      on_clause: '"session"."user_id" = "user"."id"',
      cardinality: null,
      origin: "suggested",
      inference_method: "observed_in_query_log",
      is_log_mined: true,
    },
  ],
  referenced_by: { metrics: 1, joins: 2 },
};

function renderBody(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api.meta).mockReset().mockResolvedValue(META);
  vi.mocked(api.entityColumns).mockReset();
  mockPush.mockReset();
  useSourceStore.setState({ selectedSourceId: null });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EntityDrilldownBody", () => {
  it("renders the rich header: group, name, rows, confidence pill, rationale", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue(FULL);
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    // Wait on a RichHeader-only element — the fallback heading "user" renders
    // during the loading state too, so it can't gate the rich-header assertions.
    expect(await screen.findByText("Identity")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "user" })).toBeInTheDocument();
    expect(screen.getByText("1.2M rows")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText(/primary-key \+ foreign-key evidence/)).toBeInTheDocument();
  });

  it("renders columns with category chips and the catastrophic suffix", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue(FULL);
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    await screen.findByText("password_hash");
    expect(screen.getByText("contact email")).toBeInTheDocument();
    expect(screen.getByText("CONTACT")).toBeInTheDocument();
    // The catastrophic floor category gets the explicit suffix.
    expect(screen.getByText("CREDENTIAL · CATASTROPHIC")).toBeInTheDocument();
    // The non-PII column renders the muted em-dash chip.
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("renders metrics, both join cards, and copies a paste-ready ON clause", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue(FULL);
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    await screen.findByText("active_users");
    expect(screen.getByText("subscription → user")).toBeInTheDocument();
    expect(screen.getByText("N:1")).toBeInTheDocument();
    // log-mined join with unknown cardinality shows just the mined pill.
    expect(screen.getByText("log-mined")).toBeInTheDocument();
    expect(screen.getByText('ON "subscription"."user_id" = "user"."id"')).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /copy on clause for subscription_user/i }));
    expect(await screen.findByText(/join copied/)).toBeInTheDocument();
  });

  it("flattens similar columns highest-first and discloses the hidden remainder", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue(FULL);
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    expect(await screen.findByText("public.invoice.billing_email")).toBeInTheDocument();
    expect(screen.getByText("0.94")).toBeInTheDocument();
    expect(screen.getAllByText("near email").length).toBeGreaterThan(0);
    // 7 neighbours, limit 6 → "+1 more".
    expect(screen.getByText(/\+1 more similar column/)).toBeInTheDocument();
  });

  it("navigates to the PII matrix with a focus param from the footer", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue(FULL);
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    fireEvent.click(await screen.findByRole("button", { name: /view in pii matrix/i }));
    expect(mockPush).toHaveBeenCalledWith("/pii?focus=user");
  });

  it("omits confidence + rationale for a hand-authored entity and shows empty panes", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue({
      entity: header({ confidence: null, rationale: null }),
      columns: [],
      metrics: [],
      joins: [],
      referenced_by: { metrics: 0, joins: 0 },
    });
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    await screen.findByText("No columns indexed for this entity.");
    expect(screen.queryByText("bind confidence")).not.toBeInTheDocument();
    expect(screen.queryByText(/evidence/)).not.toBeInTheDocument();
    expect(screen.getByText("No canonical joins reference this entity.")).toBeInTheDocument();
  });

  it("shows a not-found note when the entity is absent from the source", async () => {
    vi.mocked(api.entityColumns).mockRejectedValue(new Error("sidecar /api/... returned 404: not found"));
    renderBody(<EntityDrilldownBody entity="ghost" onClose={() => {}} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/isn’t bound in the current source/);
    // The header still renders the requested name.
    expect(screen.getByRole("heading", { name: "ghost" })).toBeInTheDocument();
  });

  it("calls onClose from the close button", async () => {
    vi.mocked(api.entityColumns).mockResolvedValue(FULL);
    const onClose = vi.fn();
    renderBody(<EntityDrilldownBody entity="user" onClose={onClose} />);

    fireEvent.click(await screen.findByRole("button", { name: /close entity detail/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("shows a loading state before the fetch resolves", async () => {
    vi.mocked(api.entityColumns).mockReturnValue(new Promise(() => {}));
    renderBody(<EntityDrilldownBody entity="user" onClose={() => {}} />);

    // The skeleton + the toast both carry role="status"; assert by its text.
    expect(screen.getByText(/loading entity/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "user" })).toBeInTheDocument();
    await waitFor(() => expect(api.entityColumns).toHaveBeenCalled());
  });
});
