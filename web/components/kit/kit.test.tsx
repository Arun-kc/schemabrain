import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Button } from "./Button";
import { BrainMark } from "./BrainMark";
import { ConfidenceMeter } from "./ConfidenceMeter";
import { DataTable, type DataTableColumn } from "./DataTable";
import { Eyebrow } from "./Eyebrow";
import { GlassCard } from "./GlassCard";
import { Icon, ICON_NAMES } from "./Icon";
import { IconButton } from "./IconButton";
import { PiiChip, piiCategoryToChipKind } from "./PiiChip";

describe("Icon", () => {
  it("renders an svg for every name in the registry", () => {
    for (const name of ICON_NAMES) {
      const { container, unmount } = render(<Icon name={name} />);
      expect(container.querySelector("svg")).toBeInTheDocument();
      unmount();
    }
  });

  it("is decorative (aria-hidden) without a label", () => {
    const { container } = render(<Icon name="copy" />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("exposes an accessible image role when labelled", () => {
    render(<Icon name="copy" label="Copy" />);
    const svg = screen.getByRole("img", { name: "Copy" });
    expect(svg).toBeInTheDocument();
    expect(svg).not.toHaveAttribute("aria-hidden");
  });
});

describe("BrainMark", () => {
  it("renders a token-driven svg with no hardcoded hex", () => {
    const { container } = render(<BrainMark size={32} />);
    const svg = container.querySelector("svg");
    expect(svg).toBeInTheDocument();
    expect(svg?.outerHTML).toContain("var(--green)");
    expect(svg?.outerHTML).toContain("var(--ink)");
    expect(svg?.outerHTML).not.toMatch(/#[0-9a-fA-F]{3,6}/);
  });

  it("is decorative by default and labelled when given a title", () => {
    const { container, rerender } = render(<BrainMark />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    rerender(<BrainMark title="SchemaBrain" />);
    expect(screen.getByRole("img", { name: "SchemaBrain" })).toBeInTheDocument();
  });
});

describe("Eyebrow", () => {
  it("renders the eyebrow class and content", () => {
    render(<Eyebrow>Bound entity</Eyebrow>);
    const el = screen.getByText("Bound entity");
    expect(el).toHaveClass("sb-eyebrow");
  });
});

describe("PiiChip", () => {
  it("applies the variant class", () => {
    render(<PiiChip kind="auth">SECRET</PiiChip>);
    expect(screen.getByText("SECRET")).toHaveClass("sb-chip", "auth");
  });

  it("renders a status dot when requested", () => {
    const { container } = render(<PiiChip kind="green" dot>ok</PiiChip>);
    expect(container.querySelector(".sb-chip .d")).toBeInTheDocument();
  });

  it("maps catastrophic categories to their alarm variants", () => {
    expect(piiCategoryToChipKind("auth")).toBe("auth");
    expect(piiCategoryToChipKind("payment")).toBe("payment");
    expect(piiCategoryToChipKind("contact")).toBe("contact");
    expect(piiCategoryToChipKind("anything-else")).toBe("none");
  });
});

describe("ConfidenceMeter", () => {
  it("renders the rounded percentage and exposes a meter role", () => {
    render(<ConfidenceMeter value={0.42} />);
    const meter = screen.getByRole("meter");
    expect(meter).toHaveAttribute("aria-valuenow", "42");
    expect(meter).toHaveTextContent("42%");
  });

  it("clamps out-of-range and NaN values to [0, 100]", () => {
    const { rerender } = render(<ConfidenceMeter value={1.7} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "100");
    rerender(<ConfidenceMeter value={-3} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0");
    rerender(<ConfidenceMeter value={Number.NaN} />);
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0");
  });
});

describe("GlassCard", () => {
  it("renders the solid card surface by default", () => {
    render(<GlassCard>panel</GlassCard>);
    expect(screen.getByText("panel")).toHaveClass("sb-card");
  });

  it("renders the frosted glass surface for the glass variant", () => {
    render(<GlassCard variant="glass">frost</GlassCard>);
    expect(screen.getByText("frost")).toHaveClass("sb-glass");
  });
});

describe("Button", () => {
  it("defaults to type=button so it never submits a form", () => {
    render(<Button>go</Button>);
    expect(screen.getByRole("button", { name: "go" })).toHaveAttribute("type", "button");
  });

  it("applies the variant class only for non-default variants", () => {
    const { rerender } = render(<Button>x</Button>);
    const btn = screen.getByRole("button");
    expect(btn).toHaveClass("sb-btn");
    expect(btn).not.toHaveClass("primary");
    rerender(<Button variant="primary">x</Button>);
    expect(screen.getByRole("button")).toHaveClass("sb-btn", "primary");
    rerender(<Button variant="ghost">x</Button>);
    expect(screen.getByRole("button")).toHaveClass("sb-btn", "ghost");
  });

  it("renders a leading icon and fires onClick", () => {
    const onClick = vi.fn();
    const { container } = render(
      <Button icon="copy" onClick={onClick}>
        copy
      </Button>,
    );
    expect(container.querySelector("svg")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(onClick).toHaveBeenCalledOnce();
  });
});

describe("IconButton", () => {
  it("uses the label as the accessible name and tooltip", () => {
    const { container } = render(<IconButton icon="x" label="Close" />);
    const btn = screen.getByRole("button", { name: "Close" });
    expect(btn).toHaveAttribute("title", "Close");
    expect(container.querySelector("svg")).toBeInTheDocument();
  });
});

interface Row {
  id: string;
  name: string;
}

describe("DataTable", () => {
  const columns: DataTableColumn<Row>[] = [
    { key: "name", header: "Name", cell: (r) => r.name },
  ];

  it("renders a row per item", () => {
    const rows: Row[] = [
      { id: "a", name: "users" },
      { id: "b", name: "orders" },
    ];
    render(<DataTable columns={columns} rows={rows} getRowKey={(r) => r.id} />);
    expect(screen.getByText("users")).toBeInTheDocument();
    expect(screen.getByText("orders")).toBeInTheDocument();
  });

  it("shows the empty message when there are no rows", () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        getRowKey={(r) => r.id}
        emptyMessage="Nothing here"
        caption="Entities"
      />,
    );
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(screen.getByText("Entities")).toBeInTheDocument();
  });
});
