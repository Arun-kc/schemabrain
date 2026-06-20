"use client";

import { useEffect, useState } from "react";

export type Theme = "dark" | "light";

/**
 * Reads / writes the persisted "Observatory" (dark) vs "Atlas" (light) theme.
 *
 * Mirrors the hook the landing page defines inline: the init script in
 * app/layout.tsx sets `data-theme` on <html> before hydration, so we start from
 * whatever the DOM already reflects (avoids a flash) and only persist on toggle.
 * Extracted here so /blog and any future sub-page share one source of truth.
 */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    setTheme(
      document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light",
    );
  }, []);

  const toggle = () => {
    setTheme((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      if (next === "dark") document.documentElement.setAttribute("data-theme", "dark");
      else document.documentElement.removeAttribute("data-theme");
      try {
        localStorage.setItem("schemabrain.theme", next);
      } catch {
        /* private mode / storage disabled — theme just won't persist */
      }
      return next;
    });
  };

  return { theme, toggle };
}
