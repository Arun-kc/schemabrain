import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { applyTheme, readPersistedTheme, useTheme } from "./useTheme";

const STORAGE_KEY = "schemabrain.theme";

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("readPersistedTheme", () => {
  it("defaults to light when nothing is stored", () => {
    expect(readPersistedTheme()).toBe("light");
  });

  it("returns dark when dark is persisted", () => {
    window.localStorage.setItem(STORAGE_KEY, "dark");
    expect(readPersistedTheme()).toBe("dark");
  });
});

describe("applyTheme", () => {
  it("sets data-theme=dark on the document element for dark", () => {
    applyTheme("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("removes the attribute for light (Atlas default)", () => {
    document.documentElement.setAttribute("data-theme", "dark");
    applyTheme("light");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});

describe("useTheme", () => {
  it("reads the persisted theme on mount and applies it", () => {
    window.localStorage.setItem(STORAGE_KEY, "dark");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("persists and applies an explicit setTheme", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setTheme("dark"));
    expect(result.current.theme).toBe("dark");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("flips between themes with toggleTheme", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.toggleTheme());
    expect(result.current.theme).toBe("dark");
    act(() => result.current.toggleTheme());
    expect(result.current.theme).toBe("light");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
  });
});
