import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PIICategory, PiiMatrixColumn } from "@/lib/types";

let mockSearchParams = new URLSearchParams("");
vi.mock("next/navigation", () => ({
  usePathname: () => "/pii",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => mockSearchParams,
}));

import { MatrixGrid } from "./MatrixGrid";
import styles from "./ledger.module.css";

const CATEGORIES = [
  "contact",
  "financial",
  "payment_card",
  "health",
  "genetic",
  "biometric",
  "behavioral",
  "online_identifier",
  "credential",
  "government_id",
  "location",
  "demographic_protected",
] as const satisfies readonly PIICategory[];

const COLUMNS: PiiMatrixColumn[] = [
  { entity: "user", qualified_table: "public.users", column_name: "email", sensitivity: "pii", categories: ["contact"], pii_confidence: "high" },
  { entity: "order", qualified_table: "public.orders", column_name: "total", sensitivity: "pii", categories: ["financial"], pii_confidence: "medium" },
];

function renderGrid() {
  return render(
    <MatrixGrid
      columns={COLUMNS}
      categories={CATEGORIES}
      catastrophicCategories={["credential", "payment_card", "government_id"]}
      selectedColumn={null}
      onSelect={() => {}}
    />,
  );
}

let scrollSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockSearchParams = new URLSearchParams("");
  scrollSpy = vi.fn();
  // jsdom does not implement scrollIntoView.
  Element.prototype.scrollIntoView = scrollSpy;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MatrixGrid ?focus=", () => {
  it("flashes + scrolls the focused entity's row into view", () => {
    mockSearchParams = new URLSearchParams("focus=order");
    renderGrid();

    const row = screen.getByText("total").closest("tr");
    expect(row).not.toBeNull();
    expect(row).toHaveClass(styles.focusFlash);
    expect(scrollSpy).toHaveBeenCalled();

    // The non-focused row is not flashed.
    const other = screen.getByText("email").closest("tr");
    expect(other).not.toHaveClass(styles.focusFlash);
  });

  it("does nothing when no ?focus= is present", () => {
    renderGrid();
    const row = screen.getByText("email").closest("tr");
    expect(row).not.toHaveClass(styles.focusFlash);
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it("is a no-op when ?focus= names an entity with no visible row", () => {
    mockSearchParams = new URLSearchParams("focus=ghost");
    renderGrid();

    for (const name of ["email", "total"]) {
      const row = screen.getByText(name).closest("tr");
      expect(row).not.toHaveClass(styles.focusFlash);
    }
    expect(scrollSpy).not.toHaveBeenCalled();
  });
});
