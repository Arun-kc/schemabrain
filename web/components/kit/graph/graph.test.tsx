import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import type { EdgeProps, NodeProps } from "reactflow";
import { Position, ReactFlowProvider } from "reactflow";
import { GraphEdge, type GraphEdgeData } from "./GraphEdge";
import { GraphNode, type GraphNodeData } from "./GraphNode";
import {
  GROUP_COLOR,
  graphEdgeLabel,
  graphEdgeStyle,
  graphNodeColor,
  graphNodeRing,
  graphPiiHaloColor,
} from "./graphStyle";

describe("graphStyle", () => {
  it("maps every group to an accent colour token", () => {
    expect(GROUP_COLOR.identity).toContain("--green");
    expect(GROUP_COLOR.billing).toContain("--cyan");
    expect(GROUP_COLOR.activity).toContain("--violet");
    expect(GROUP_COLOR.other).toContain("--ink-3");
  });

  it("uses the reserved alarm colour for catastrophic nodes", () => {
    expect(graphNodeColor("identity")).toBe("var(--green)");
    expect(graphNodeColor("identity", true)).toBe("var(--alarm)");
  });

  it("rings catastrophic over the group accent (selection is a separate outer ring)", () => {
    expect(graphNodeRing("billing", true)).toBe("var(--alarm)");
    expect(graphNodeRing("billing", false)).toBe("var(--cyan)");
    expect(graphNodeRing("identity", false)).toBe("var(--green)");
  });

  it("styles edges by priority and reflects declared-vs-mined dashing", () => {
    expect(graphEdgeStyle({ declared: true }).strokeDasharray).toBeUndefined();
    expect(graphEdgeStyle({ declared: false }).strokeDasharray).toBe("4 5");

    const path = graphEdgeStyle({ declared: true, highlighted: true });
    expect(path.stroke).toBe("var(--green)");
    expect(path.strokeWidth).toBe(2.6);

    const mined = graphEdgeStyle({ declared: false, minedEmphasis: true });
    expect(mined.stroke).toBe("var(--cyan)");
    expect(mined.strokeDasharray).toBe("4 5");

    const resting = graphEdgeStyle({ declared: true });
    expect(resting.stroke).toBe("var(--hair)");
    expect(resting.opacity).toBe(0.5);
  });

  it("fades a resting edge under a focus overlay, but never an emphasised one", () => {
    expect(graphEdgeStyle({ declared: true, dimmed: true }).opacity).toBeLessThan(0.5);
    // Highlighted / mined edges stay prominent even when dimming is requested.
    expect(graphEdgeStyle({ declared: true, highlighted: true, dimmed: true }).opacity).toBe(0.95);
    expect(graphEdgeStyle({ declared: false, minedEmphasis: true, dimmed: true }).opacity).toBe(
      0.9,
    );
  });

  it("emphasises edges incident to the selected node (G3), below path/mined", () => {
    const sel = graphEdgeStyle({ declared: true, selectedIncident: true });
    expect(sel.strokeWidth).toBe(2);
    expect(sel.opacity).toBe(0.95);
    // A brighter NEUTRAL (ink, not green/cyan) — clearly visible without
    // implying a relationship type.
    expect(sel.stroke).toBe("var(--ink-2)");
    // A selected incident edge ignores dimming (it is the focus).
    expect(graphEdgeStyle({ declared: true, selectedIncident: true, dimmed: true }).opacity).toBe(
      0.95,
    );
    // Priority: path > mined > selected.
    expect(
      graphEdgeStyle({ declared: true, highlighted: true, selectedIncident: true }).stroke,
    ).toBe("var(--green)");
    expect(
      graphEdgeStyle({ declared: false, minedEmphasis: true, selectedIncident: true }).stroke,
    ).toBe("var(--cyan)");
  });

  it("glows ONLY the highlighted canonical-path edge (G9)", () => {
    expect(graphEdgeStyle({ declared: true, highlighted: true }).filter).toContain("drop-shadow");
    // Resting, selected, and mined edges carry no glow.
    expect(graphEdgeStyle({ declared: true }).filter).toBeUndefined();
    expect(graphEdgeStyle({ declared: true, selectedIncident: true }).filter).toBeUndefined();
    expect(graphEdgeStyle({ declared: false, minedEmphasis: true }).filter).toBeUndefined();
  });
});

describe("graphEdgeLabel", () => {
  it("labels the log-mined overlay 'mined' in cyan (no fabricated cardinality)", () => {
    expect(graphEdgeLabel({ minedEmphasis: true, cardinality: null })).toEqual({
      text: "mined",
      color: "var(--cyan)",
    });
  });

  it("labels a highlighted path edge with its declared cardinality in green", () => {
    expect(graphEdgeLabel({ highlighted: true, cardinality: "one_to_many" })).toEqual({
      text: "1:N",
      color: "var(--green)",
    });
  });

  it("labels a selected incident edge with its cardinality in the muted ink tone", () => {
    expect(graphEdgeLabel({ selectedIncident: true, cardinality: "many_to_one" })).toEqual({
      text: "N:1",
      color: "var(--ink-2)",
    });
  });

  it("shows NO label when the edge is at rest", () => {
    expect(graphEdgeLabel({ cardinality: "one_to_one" })).toBeNull();
  });

  it("shows NO label when emphasised but the cardinality is unknown (honest)", () => {
    // A selected/highlighted edge whose cardinality is null (e.g. a mined join
    // off the overlay) must not invent a cardinality — it simply has no label.
    expect(graphEdgeLabel({ selectedIncident: true, cardinality: null })).toBeNull();
    expect(graphEdgeLabel({ highlighted: true, cardinality: null })).toBeNull();
  });

  it("halos only the real-PII tiers, never sensitivity bands", () => {
    expect(graphPiiHaloColor("catastrophic")).toBe("var(--alarm)");
    expect(graphPiiHaloColor("pii")).toBe("var(--pii-2)");
    // confidential / internal are sensitivity, not PII → no halo.
    expect(graphPiiHaloColor("confidential")).toBeNull();
    expect(graphPiiHaloColor("internal")).toBeNull();
    expect(graphPiiHaloColor("none")).toBeNull();
  });
});

function nodeProps(data: GraphNodeData, selected = false): NodeProps<GraphNodeData> {
  return { data, selected } as unknown as NodeProps<GraphNodeData>;
}

// GraphNode now renders reactflow <Handle>s, which read the reactflow store —
// so the node must mount inside a ReactFlowProvider (the same context the real
// graph surface supplies). Standalone renders would throw without it.
function renderNode(ui: ReactElement) {
  return render(<ReactFlowProvider>{ui}</ReactFlowProvider>);
}

describe("GraphNode", () => {
  it("renders the entity label and group attribute", () => {
    renderNode(<GraphNode {...nodeProps({ label: "users", group: "identity" })} />);
    const node = screen.getByText("users").closest(".sb-gnode");
    expect(node).toHaveAttribute("data-group", "identity");
    expect(node).not.toHaveAttribute("data-catastrophic");
  });

  it("flags catastrophic entities distinctly", () => {
    renderNode(
      <GraphNode
        {...nodeProps({ label: "card_vault", group: "billing", catastrophic: true })}
      />,
    );
    expect(screen.getByText("CATASTROPHIC")).toBeInTheDocument();
    expect(screen.getByText("card_vault").closest(".sb-gnode")).toHaveAttribute(
      "data-catastrophic",
      "true",
    );
  });

  it("renders a bright group-coloured core dot inside the orb", () => {
    const { container } = renderNode(
      <GraphNode {...nodeProps({ label: "users", group: "identity" })} />,
    );
    expect(container.querySelector('[data-core="true"]')).not.toBeNull();
  });

  it("draws a distinct OUTER selection ring on select — even on catastrophic nodes (G3)", () => {
    const { container, rerender } = renderNode(
      <GraphNode {...nodeProps({ label: "user", group: "billing", catastrophic: true }, false)} />,
    );
    expect(container.querySelector('[data-selected-ring="true"]')).toBeNull();

    rerender(
      <ReactFlowProvider>
        <GraphNode
          {...nodeProps({ label: "user", group: "billing", catastrophic: true }, true)}
        />
      </ReactFlowProvider>,
    );
    // The alarm ring still wins the orb border; the green selection ring is an
    // additive OUTER ring so the pick is unmistakable on a catastrophic node.
    expect(container.querySelector('[data-selected-ring="true"]')).not.toBeNull();
    expect(screen.getByText("user").closest(".sb-gnode")).toHaveAttribute(
      "data-catastrophic",
      "true",
    );
  });

  it("renders the PII halo only when the heat overlay is on AND the entity carries PII", () => {
    const { container, rerender } = renderNode(
      <GraphNode {...nodeProps({ label: "users", group: "identity", piiLevel: "pii" })} />,
    );
    // Overlay off → no halo even though the entity carries PII.
    expect(container.querySelector('[data-pii-halo="true"]')).toBeNull();

    rerender(
      <ReactFlowProvider>
        <GraphNode
          {...nodeProps({ label: "users", group: "identity", piiLevel: "pii", piiHeat: true })}
        />
      </ReactFlowProvider>,
    );
    expect(container.querySelector('[data-pii-halo="true"]')).not.toBeNull();

    // Heat on but a non-PII tier → still no halo.
    rerender(
      <ReactFlowProvider>
        <GraphNode
          {...nodeProps({
            label: "users",
            group: "identity",
            piiLevel: "internal",
            piiHeat: true,
          })}
        />
      </ReactFlowProvider>,
    );
    expect(container.querySelector('[data-pii-halo="true"]')).toBeNull();
  });

  it("renders the refusal badge only when the overlay is on AND the count is positive", () => {
    const { container, rerender } = renderNode(
      <GraphNode
        {...nodeProps({ label: "order", group: "activity", refusalCount: 3, refusalActive: true })}
      />,
    );
    const badge = container.querySelector('[data-refusal-badge="true"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toBe("3");

    // Overlay on but zero refusals → no badge.
    rerender(
      <ReactFlowProvider>
        <GraphNode
          {...nodeProps({
            label: "order",
            group: "activity",
            refusalCount: 0,
            refusalActive: true,
          })}
        />
      </ReactFlowProvider>,
    );
    expect(container.querySelector('[data-refusal-badge="true"]')).toBeNull();

    // Refusals present but overlay off → no badge.
    rerender(
      <ReactFlowProvider>
        <GraphNode
          {...nodeProps({ label: "order", group: "activity", refusalCount: 3 })}
        />
      </ReactFlowProvider>,
    );
    expect(container.querySelector('[data-refusal-badge="true"]')).toBeNull();
  });

  it("renders the refusal pulse-ring alongside the badge when the hotspot is live (G4)", () => {
    const { container, rerender } = renderNode(
      <GraphNode
        {...nodeProps({ label: "user", group: "identity", refusalCount: 2, refusalActive: true })}
      />,
    );
    // The pulse ring (CSS-animated, reduced-motion gated) accompanies the badge.
    expect(container.querySelector('[data-refusal-pulse="true"]')).not.toBeNull();

    // Overlay off → no pulse (resting node is unchanged).
    rerender(
      <ReactFlowProvider>
        <GraphNode {...nodeProps({ label: "user", group: "identity", refusalCount: 2 })} />
      </ReactFlowProvider>,
    );
    expect(container.querySelector('[data-refusal-pulse="true"]')).toBeNull();

    // Overlay on but zero refusals → no pulse.
    rerender(
      <ReactFlowProvider>
        <GraphNode
          {...nodeProps({
            label: "user",
            group: "identity",
            refusalCount: 0,
            refusalActive: true,
          })}
        />
      </ReactFlowProvider>,
    );
    expect(container.querySelector('[data-refusal-pulse="true"]')).toBeNull();
  });
});

function edgeProps(data: GraphEdgeData): EdgeProps<GraphEdgeData> {
  return {
    id: "e1",
    source: "a",
    target: "b",
    sourceX: 0,
    sourceY: 0,
    targetX: 100,
    targetY: 100,
    sourcePosition: Position.Bottom,
    targetPosition: Position.Top,
    data,
  } as unknown as EdgeProps<GraphEdgeData>;
}

describe("GraphEdge", () => {
  it("renders a declared FK as a solid path", () => {
    const { container } = render(
      <svg>
        <GraphEdge {...edgeProps({ declared: true })} />
      </svg>,
    );
    const path = container.querySelector("path");
    expect(path).toBeInTheDocument();
    expect(path?.getAttribute("style") ?? "").not.toContain("dasharray");
  });

  it("renders a log-mined join as a dashed path", () => {
    const { container } = render(
      <svg>
        <GraphEdge {...edgeProps({ declared: false, minedEmphasis: true })} />
      </svg>,
    );
    const path = container.querySelector("path");
    expect(path?.getAttribute("style") ?? "").toContain("4 5");
  });

  it("draws the cardinality label on a highlighted path edge (G1)", () => {
    const { container } = render(
      <svg>
        <GraphEdge
          {...edgeProps({ declared: true, highlighted: true, cardinality: "many_to_one" })}
        />
      </svg>,
    );
    expect(container.querySelector("text")?.textContent).toBe("N:1");
  });

  it("labels a log-mined emphasised edge 'mined', never a fabricated cardinality", () => {
    const { container } = render(
      <svg>
        <GraphEdge
          {...edgeProps({ declared: false, minedEmphasis: true, cardinality: null })}
        />
      </svg>,
    );
    expect(container.querySelector("text")?.textContent).toBe("mined");
  });

  it("draws NO label on a resting edge", () => {
    const { container } = render(
      <svg>
        <GraphEdge {...edgeProps({ declared: true, cardinality: "one_to_one" })} />
      </svg>,
    );
    expect(container.querySelector("text")).toBeNull();
  });
});
