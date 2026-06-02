import type { Config } from "tailwindcss";

/**
 * Tailwind 4 configuration for the SchemaBrain dashboard.
 *
 * Design tokens are declared as CSS custom properties in
 * `app/globals.css` per the project's web rules (see
 * `~/.claude/rules/web/coding-style.md` "CSS Custom Properties").
 * Tailwind references the variables via `theme()` extensions so
 * a contributor changing a colour in one place updates the whole
 * surface. Anti-template enforcement (see `web/design-quality.md`)
 * lives in component CSS, not here.
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        ink: {
          200: "var(--color-ink-200)",
          300: "var(--color-ink-300)",
          500: "var(--color-ink-500)",
          700: "var(--color-ink-700)",
          900: "var(--color-ink-900)",
        },
        ivory: {
          50: "var(--color-ivory-50)",
          100: "var(--color-ivory-100)",
        },
        signal: {
          green: "var(--color-signal-green)",
          amber: "var(--color-signal-amber)",
          red: "var(--color-signal-red)",
        },
      },
      fontFamily: {
        display: "var(--font-display)",
        body: "var(--font-body)",
        mono: "var(--font-mono)",
      },
      // NOTE: spacing (gap-rhythm-*, py-section) and the typographic scale are
      // generated directly from the `@theme` tokens in app/globals.css
      // (--spacing-rhythm-*, --spacing-section). The previous `spacing`/`fontSize`
      // blocks here pointed at `--space-*`/`--text-*` vars that never existed —
      // dead config that could only shadow the working @theme utilities. Removed.
      transitionTimingFunction: {
        "out-expo": "var(--ease-out-expo)",
      },
      transitionDuration: {
        fast: "var(--duration-fast)",
        normal: "var(--duration-normal)",
      },
    },
  },
  plugins: [],
};

export default config;
