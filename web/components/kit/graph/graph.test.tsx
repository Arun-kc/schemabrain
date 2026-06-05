import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import type { EdgeProps, NodeProps } from "reactflow";
import { Position, ReactFlowProvider } from "reactflow";
import { GraphEdge, type GraphEdgeData } from "./GraphEdge";
import { GraphNode, type GraphNodeData } from "./GraphNode";
import {
  GROUP_COLOR,
  graphEdgeStyle,
  graphNodeColor,
  graphNodeRing,
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

  it("rings catastrophic > selected > group", () => {
    expect(graphNodeRing("billing", true, true)).toBe("var(--alarm)");
    expect(graphNodeRing("billing", false, true)).toBe("var(--green)");
    expect(graphNodeRing("billing", false, false)).toBe("var(--cyan)");
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
});
