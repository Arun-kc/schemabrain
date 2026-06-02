import { describe, expect, it, vi } from "vitest";
import { act, render, renderHook, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { ToastProvider, useToast } from "./Toast";

function wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

describe("useToast", () => {
  it("throws when used outside a ToastProvider", () => {
    // Silence the expected React error boundary log noise.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => renderHook(() => useToast())).toThrow(/ToastProvider/);
    spy.mockRestore();
  });

  it("opens the toast with the message and auto-hides it", () => {
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useToast(), { wrapper });
      act(() => result.current.showToast("hello"));
      const toast = screen.getByRole("status");
      expect(toast).toHaveAttribute("data-open", "true");
      expect(toast).toHaveTextContent("hello");
      act(() => vi.advanceTimersByTime(2000));
      expect(screen.getByRole("status")).toHaveAttribute("data-open", "false");
    } finally {
      vi.useRealTimers();
    }
  });

  it("copies text to the clipboard and toasts the label", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const { result } = renderHook(() => useToast(), { wrapper });
    await act(async () => {
      await result.current.copyToClipboard("ON a.id = b.a_id", "Copied join");
    });
    expect(writeText).toHaveBeenCalledWith("ON a.id = b.a_id");
    expect(screen.getByRole("status")).toHaveTextContent("Copied join");
  });
});

describe("ToastProvider", () => {
  it("renders its children", () => {
    render(
      <ToastProvider>
        <span>child content</span>
      </ToastProvider>,
    );
    expect(screen.getByText("child content")).toBeInTheDocument();
  });
});
