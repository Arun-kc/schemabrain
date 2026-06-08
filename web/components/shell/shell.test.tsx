import {
  type AnchorHTMLAttributes,
  type ReactNode,
} from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Meta } from "@/lib/types/meta";

// --- module mocks ---------------------------------------------------------

const mockReplace = vi.fn();
const mockPush = vi.fn();
let mockPathname = "/pii";
let mockSearchParams = new URLSearchParams("");

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ replace: mockReplace, push: mockPush }),
  useSearchParams: () => mockSearchParams,
}));

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children?: ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", () => ({ api: { meta: vi.fn(), entityColumns: vi.fn() } }));

import { api } from "@/lib/api";
import type { EntityDrilldownResponse } from "@/lib/types/meta";
import { useSourceStore } from "@/lib/sourceStore";
import { AppShell } from "./AppShell";
import { CommandPalette } from "./CommandPalette";
import { DrilldownSheet } from "./DrilldownSheet";
import { NavRail } from "./NavRail";
import { SourceSelector } from "./SourceSelector";
import { TopBar } from "./TopBar";
import { NAV, isNavItemActive } from "./nav";

const META: Meta = {
  charter_version: "1.2",
  dashboard_schema_version: "1.0",
  fingerprint_version: "fp-v1",
  store_path: "/tmp/store.db",
  default_source_connection_id: "src_default",
  source_connection_ids: ["src_default", "src_b"],
  sources: [
    { source_id: "src_default", engine: "postgres", state: "indexed", last_indexed_at: 1, tables: 3, entities: 2 },
    { source_id: "src_b", engine: null, state: "indexed", last_indexed_at: null, tables: 1, entities: 0 },
  ],
};

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const DRILLDOWN: EntityDrilldownResponse = {
  entity: {
    name: "customers",
    description: "",
    qualified_table: "public.customers",
    identity: "id",
    group: "identity",
    origin: "suggested",
    inference_method: "llm_suggested",
    validation_state: "applied",
    rows: 1000,
    confidence: "high",
    rationale: null,
  },
  columns: [],
  metrics: [],
  joins: [],
  referenced_by: { metrics: 0, joins: 0 },
};

beforeEach(() => {
  vi.mocked(api.meta).mockReset();
  vi.mocked(api.meta).mockResolvedValue(META);
  vi.mocked(api.entityColumns).mockReset();
  vi.mocked(api.entityColumns).mockResolvedValue(DRILLDOWN);
  mockReplace.mockReset();
  mockPush.mockReset();
  mockPathname = "/pii";
  mockSearchParams = new URLSearchParams("");
  useSourceStore.setState({ selectedSourceId: null });
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("nav model", () => {
  it("matches the active route exactly or as a parent segment", () => {
    const item = { id: "pii", href: "/pii", label: "PII", icon: "shield", built: true } as const;
    expect(isNavItemActive(item, "/pii")).toBe(true);
    expect(isNavItemActive(item, "/pii/extra")).toBe(true);
    expect(isNavItemActive(item, "/audit")).toBe(false);
  });

  it("ships all nine handoff surfaces, every one built", () => {
    const surfaces = ["overview", "graph", "entities", "dict", "pii", "refusals", "audit", "policy", "drift"];
    const ids = NAV.flatMap((g) => g.items.map((i) => i.id));
    expect(ids).toEqual(surfaces);
    const built = NAV.flatMap((g) => g.items).filter((i) => i.built).map((i) => i.id);
    expect(built).toEqual(surfaces);
  });
});

describe("SourceSelector", () => {
  it("reflects the resolved source and selects another", async () => {
    renderWithClient(<SourceSelector />);
    const trigger = await screen.findByRole("button", { name: /active source: src_default/ });

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("menuitemradio", { name: /src_b/ }));

    expect(useSourceStore.getState().selectedSourceId).toBe("src_b");
  });

  it("disables the trigger when no source is indexed", async () => {
    vi.mocked(api.meta).mockResolvedValue({ ...META, sources: [], source_connection_ids: [], default_source_connection_id: null });
    renderWithClient(<SourceSelector />);
    const trigger = await screen.findByRole("button", { name: /active source: no source/ });
    expect(trigger).toBeDisabled();
  });
});

describe("NavRail", () => {
  it("marks the active built surface and links every shipped surface", async () => {
    renderWithClient(<NavRail />);

    const pii = await screen.findByRole("link", { name: /PII matrix/ });
    expect(pii).toHaveAttribute("aria-current", "page");
    expect(pii).toHaveAttribute("href", "/pii");

    // Graph + Entities ship → live links.
    const graph = await screen.findByRole("link", { name: /Knowledge graph/ });
    expect(graph).toHaveAttribute("href", "/graph");
    const entities = await screen.findByRole("link", { name: /Entities/ });
    expect(entities).toHaveAttribute("href", "/entities");

    // Data dictionary now ships → a live link, no longer disabled (PR-19).
    const dict = await screen.findByRole("link", { name: /Data dictionary/ });
    expect(dict).toHaveAttribute("href", "/dict");
    expect(dict.closest(".sb-nav-item")).not.toHaveAttribute("aria-disabled", "true");
  });
});

describe("TopBar", () => {
  it("opens search and toggles theme", async () => {
    const onSearch = vi.fn();
    renderWithClient(<TopBar onSearch={onSearch} />);

    fireEvent.click(screen.getByRole("button", { name: /open command palette/i }));
    expect(onSearch).toHaveBeenCalledOnce();

    expect(document.documentElement.getAttribute("data-theme")).not.toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: /switch to dark theme/i }));
    await waitFor(() =>
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark"),
    );
  });
});

describe("CommandPalette", () => {
  it("renders nothing when closed", () => {
    const { container } = render(<CommandPalette open={false} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a dialog and closes on Escape", () => {
    const onClose = vi.fn();
    render(<CommandPalette open onClose={onClose} />);
    expect(screen.getByRole("dialog", { name: /command palette/i })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("DrilldownSheet", () => {
  it("opens when ?entity= is present and closes by dropping the param", () => {
    mockSearchParams = new URLSearchParams("entity=customers");
    // The body fetches lazily — render under a QueryClient. The header renders
    // the entity name immediately (before the fetch resolves), so the assertion
    // does not race the query.
    const { container } = renderWithClient(<DrilldownSheet />);

    const sheet = container.querySelector(".sb-drill");
    expect(sheet).toHaveClass("show");
    expect(sheet).not.toHaveAttribute("inert");
    expect(screen.getByRole("heading", { name: "customers" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /close entity detail/i }));
    expect(mockReplace).toHaveBeenCalledWith("/pii");
  });

  it("is closed and inert without an ?entity= param", () => {
    mockSearchParams = new URLSearchParams("");
    const { container } = renderWithClient(<DrilldownSheet />);
    const sheet = container.querySelector(".sb-drill");
    expect(sheet).not.toHaveClass("show");
    expect(sheet).toHaveAttribute("inert");
    // No body mounts while closed → no fetch fires.
    expect(api.entityColumns).not.toHaveBeenCalled();
  });
});

describe("AppShell", () => {
  it("renders children and toggles the command palette on Cmd-K", async () => {
    renderWithClient(
      <AppShell>
        <p>surface body</p>
      </AppShell>,
    );

    expect(await screen.findByText("surface body")).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /command palette/i })).toBeNull();

    fireEvent.keyDown(document, { key: "k", metaKey: true });
    expect(screen.getByRole("dialog", { name: /command palette/i })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "k", metaKey: true });
    expect(screen.queryByRole("dialog", { name: /command palette/i })).toBeNull();
  });
});
