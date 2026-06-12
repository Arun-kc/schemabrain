import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useFocusTrap } from "./useFocusTrap";

function Harness({ active, onEscape }: { active: boolean; onEscape: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(active, ref, onEscape);
  return (
    <div>
      <button type="button">outside</button>
      <div ref={ref}>
        <button type="button">first</button>
        <button type="button">last</button>
      </div>
    </div>
  );
}

describe("useFocusTrap", () => {
  it("focuses the first focusable when activated", () => {
    render(<Harness active onEscape={() => {}} />);
    expect(screen.getByText("first")).toHaveFocus();
  });

  it("calls onEscape on Escape", () => {
    const onEscape = vi.fn();
    render(<Harness active onEscape={onEscape} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onEscape).toHaveBeenCalledOnce();
  });

  it("wraps Tab at the edges", () => {
    render(<Harness active onEscape={() => {}} />);
    const first = screen.getByText("first");
    const last = screen.getByText("last");

    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(first).toHaveFocus();

    first.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
  });

  it("does nothing while inactive", () => {
    const onEscape = vi.fn();
    render(<Harness active={false} onEscape={onEscape} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onEscape).not.toHaveBeenCalled();
  });

  it("returns focus to the opener on deactivate", () => {
    const { rerender } = render(<Harness active={false} onEscape={() => {}} />);
    const outside = screen.getByText("outside");
    outside.focus();

    rerender(<Harness active onEscape={() => {}} />);
    expect(screen.getByText("first")).toHaveFocus();

    rerender(<Harness active={false} onEscape={() => {}} />);
    expect(outside).toHaveFocus();
  });

  it("no-ops Tab when the container has no focusables", () => {
    function Empty() {
      const ref = useRef<HTMLDivElement>(null);
      useFocusTrap(true, ref, () => {});
      return <div ref={ref} />;
    }
    render(<Empty />);
    // No focusables to cycle — must not throw.
    expect(() => fireEvent.keyDown(document, { key: "Tab" })).not.toThrow();
  });

  it("is inert when the ref never attaches", () => {
    function Detached() {
      const ref = useRef<HTMLDivElement>(null);
      useFocusTrap(true, ref, () => {});
      return <div />;
    }
    expect(() => render(<Detached />)).not.toThrow();
  });
});
