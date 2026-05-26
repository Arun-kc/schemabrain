"use client";

import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import styles from "./theme-toggle.module.css";

type Theme = "light" | "dark";
const STORAGE_KEY = "schemabrain.theme";

function readPersistedTheme(): Theme {
  if (typeof window === "undefined") return "light";
  return window.localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
}

function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  if (theme === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setTheme(readPersistedTheme());
    setMounted(true);
  }, []);

  function pick(next: Theme) {
    setTheme(next);
    applyTheme(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* private browsing — non-persistent toggle still works */
    }
  }

  const displayTheme = mounted ? theme : "light";

  return (
    <div role="group" aria-label="theme" className={styles.group}>
      <button
        type="button"
        onClick={() => pick("light")}
        aria-pressed={displayTheme === "light"}
        className={cn(styles.btn, displayTheme === "light" && styles.active)}
      >
        light
      </button>
      <span className={styles.divider} aria-hidden="true" />
      <button
        type="button"
        onClick={() => pick("dark")}
        aria-pressed={displayTheme === "dark"}
        className={cn(styles.btn, displayTheme === "dark" && styles.active)}
      >
        dark
      </button>
    </div>
  );
}
