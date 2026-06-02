"use client";

import { cn } from "@/lib/cn";
import { useTheme } from "@/lib/useTheme";
import styles from "./theme-toggle.module.css";

export function ThemeToggle() {
  // Theme read/apply/persist is owned by the shared useTheme hook (single
  // source of truth, no key={theme} remount). This component is just the UI.
  const { theme: displayTheme, setTheme: pick } = useTheme();

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
