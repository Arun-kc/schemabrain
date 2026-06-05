import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { CanonicalPath } from "@/lib/types/graph";
import { GraphIndexingOverlay } from "./GraphIndexingOverlay";
import { GraphLegend } from "./GraphLegend";
import { GraphPath } from "./GraphPath";
import { GraphTooltip } from "./GraphTooltip";
import { GraphTools, type OverlayState } from "./GraphTools";

const NO_OVERLAYS: OverlayState = { pii: false, refusals: false, mined: false };

describe("GraphTools", () => {
  it("renders the search input and three data-backed overlay toggles", () => {
    render(
      <GraphTools
        search=""
        onSearchChange={() => {}}
        overlays={NO_OVERLAYS}
        onToggle={() => {}}
        unattributedRefusals={0}
      />,
    );
    expect(screen.getByRole("searchbox", { name: /search entities/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "PII heat" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refusal hotspots" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log-mined joins" })).toBeInTheDocument();
  });

  it("reports the toggled overlay key and search text", () => {
    const onToggle = vi.fn();
    const onSearchChange = vi.fn();
    render(
      <GraphTools
        search=""
        onSearchChange={onSearchChange}
        overlays={NO_OVERLAYS}
        onToggle={onToggle}
        unattributedRefusals={0}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Log-mined joins" }));
    expect(onToggle).toHaveBeenCalledWith("mined");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "user" } });
    expect(onSearchChange).toHaveBeenCalledWith("user");
  });

  it("reflects pressed state via aria-pressed", () => {
    render(
      <GraphTools
        search=""
        onSearchChange={() => {}}
        overlays={{ pii: true, refusals: false, mined: false }}
        onToggle={() => {}}
        unattributedRefusals={0}
      />,
    );
    expect(screen.getByRole("button", { name: "PII heat" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("surfaces the unattributed remainder ONLY when the refusal overlay is on", () => {
    const { rerender } = render(
      <GraphTools
        search=""
        onSearchChange={() => {}}
        overlays={NO_OVERLAYS}
        onToggle={() => {}}
        unattributedRefusals={3}
      />,
    );
    // Overlay off → no note even though there are unattributed refusals.
    expect(screen.queryByText(/not attributed/i)).toBeNull();

    rerender(
      <GraphTools
        search=""
        onSearchChange={() => {}}
        overlays={{ pii: false, refusals: true, mined: false }}
        onToggle={() => {}}
        unattributedRefusals={3}
      />,
    );
    expect(screen.getByText(/3 refusals not attributed to a visible entity/i)).toBeInTheDocument();

    // On but zero unattributed → no note.
    rerender(
      <GraphTools
        search=""
        onSearchChange={() => {}}
        overlays={{ pii: false, refusals: true, mined: false }}
        onToggle={() => {}}
        unattributedRefusals={0}
      />,
    );
    expect(screen.queryByText(/not attributed/i)).toBeNull();
  });

  it("singularises a single unattributed refusal", () => {
    render(
      <GraphTools
        search=""
        onSearchChange={() => {}}
        overlays={{ pii: false, refusals: true, mined: false }}
        onToggle={() => {}}
        unattributedRefusals={1}
      />,
    );
    expect(screen.getByText(/1 refusal not attributed to a visible entity/i)).toBeInTheDocument();
  });
});

describe("GraphLegend", () => {
  it("renders the legend rows and copies the re-index command on click", () => {
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<GraphLegend hasMinedEdges={false} />);

    expect(screen.getByText("Catastrophic PII")).toBeInTheDocument();
    expect(screen.getByText("Declared FK")).toBeInTheDocument();

    const button = screen.getByRole("button", { name: /copy re-index command/i });
    fireEvent.click(button);
    expect(writeText).toHaveBeenCalledWith("schemabrain index");
    expect(screen.getByRole("button", { name: /copied/i })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("shows the provenance footnote only when the schema has mined edges", () => {
    const { rerender } = render(<GraphLegend hasMinedEdges={false} />);
    expect(screen.queryByText(/recovered from query logs/i)).toBeNull();
    rerender(<GraphLegend hasMinedEdges={true} />);
    expect(screen.getByText(/recovered from query logs/i)).toBeInTheDocument();
  });
});

describe("GraphPath", () => {
  it("renders the ordered hops and the hop count from the wire", () => {
    const path: CanonicalPath = {
      nodes: ["order_item", "order", "user", "tenant"],
      edges: ["a", "b", "c"],
      hops: 3,
    };
    render(<GraphPath path={path} />);
    expect(screen.getByText("Canonical path · 3 hops")).toBeInTheDocument();
    for (const id of path.nodes) expect(screen.getByText(id)).toBeInTheDocument();
  });

  it("handles an empty spine (hops 0) without rendering an arrow run", () => {
    render(<GraphPath path={{ nodes: [], edges: [], hops: 0 }} />);
    expect(screen.getByText(/no multi-hop join chain yet/i)).toBeInTheDocument();
    expect(screen.queryByText("→")).toBeNull();
  });

  it("singularises a one-hop path", () => {
    render(<GraphPath path={{ nodes: ["a", "b"], edges: ["x"], hops: 1 }} />);
    expect(screen.getByText("Canonical path · 1 hop")).toBeInTheDocument();
  });
});

describe("GraphTooltip", () => {
  it("renders the label, a humanised row count, and the PII summary", () => {
    render(<GraphTooltip label="user" rowCount={1200} piiLevel="catastrophic" x={10} y={20} />);
    expect(screen.getByRole("tooltip")).toHaveTextContent("user");
    expect(screen.getByText(/1,200 rows · catastrophic PII/)).toBeInTheDocument();
  });

  it("renders an em dash for a null row count and 'no PII' for none", () => {
    render(<GraphTooltip label="t" rowCount={null} piiLevel="none" x={0} y={0} />);
    expect(screen.getByText(/— rows · no PII/)).toBeInTheDocument();
  });

  it("labels the middle PII tier", () => {
    render(<GraphTooltip label="p" rowCount={0} piiLevel="pii" x={0} y={0} />);
    expect(screen.getByText(/0 rows · PII present/)).toBeInTheDocument();
  });

  it("labels the sensitivity-only tiers without calling them PII", () => {
    const { rerender } = render(
      <GraphTooltip label="c" rowCount={5} piiLevel="confidential" x={0} y={0} />,
    );
    expect(screen.getByText(/5 rows · confidential/)).toBeInTheDocument();
    rerender(<GraphTooltip label="i" rowCount={5} piiLevel="internal" x={0} y={0} />);
    expect(screen.getByText(/5 rows · internal/)).toBeInTheDocument();
  });
});

describe("GraphIndexingOverlay", () => {
  it("renders honest phase copy with no fabricated percentage", () => {
    render(<GraphIndexingOverlay sourceLabel="prod" />);
    expect(screen.getByRole("status")).toHaveTextContent("Indexing prod…");
    expect(screen.getByText(/mining joins from query logs/i)).toBeInTheDocument();
    expect(screen.queryByText(/%/)).toBeNull();
  });

  it("falls back to a generic label when the source id is unknown", () => {
    render(<GraphIndexingOverlay sourceLabel={null} />);
    expect(screen.getByRole("status")).toHaveTextContent("Indexing this source…");
  });
});
