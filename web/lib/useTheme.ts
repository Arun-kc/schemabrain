"use client";

import { useCallback, useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "schemabrain.theme";

/** Read the persisted theme (Atlas/light is the default). SSR-safe. */
export function readPersistedTheme(): Theme {
  if (typeof window === "undefined") return "light";
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

/**
 * Apply a theme by toggling `data-theme` on <html> — the single attribute the
 * token system keys off. Deliberately NOT a React-rendered wrapper with
 * `key={theme}` (the prototype's approach), which remounts the whole tree and
 * discards scroll, focus, and in-flight state on every toggle.
 *
 * After the attribute flips we force one synchronous style/layout recalc so
 * backdrop-filter (.sb-glass) surfaces repaint against the new theme
 * immediately rather than lagging a frame behind the token swap.
 */
export function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (theme === "dark") {
    root.setAttribute("data-theme", "dark");
  } else {
    root.removeAttribute("data-theme");
  }
  // Reading a layout property forces the recalc that repaints glass surfaces.
  void root.offsetHeight;
}

interface UseThemeResult {
  /** The active theme; "light" until mounted to match SSR output. */
  theme: Theme;
  /** True once the persisted theme has been read on the client. */
  mounted: boolean;
  setTheme: (next: Theme) => void;
  toggleTheme: () => void;
}

/**
 * Single source of truth for the dashboard theme. Reads the persisted choice
 * on mount, applies it to <html>, and persists changes. The inline init script
 * in app/layout.tsx applies the stored theme before hydration to avoid a flash;
 * this hook owns every subsequent change.
 */
export function useTheme(): UseThemeResult {
  const [theme, setThemeState] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const persisted = readPersistedTheme();
    setThemeState(persisted);
    // Re-apply on mount so the DOM is authoritative even if the pre-hydration
    // init script in layout.tsx is absent (e.g. a future host) — same value it
    // already set, so no flash.
    applyTheme(persisted);
    setMounted(true);
  }, []);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    applyTheme(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Private browsing / storage disabled — the toggle still works for the
      // session, it just won't persist across reloads.
    }
  }, []);

  const toggleTheme = useCallback(() => {
    setThemeState((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch {
        /* non-persistent fallback */
      }
      return next;
    });
  }, []);

  return { theme: mounted ? theme : "light", mounted, setTheme, toggleTheme };
}
