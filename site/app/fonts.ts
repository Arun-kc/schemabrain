import localFont from "next/font/local";

/**
 * Self-hosted typography for the SchemaBrain dashboard.
 *
 * The design handoff specifies three families — Space Grotesk (display),
 * JetBrains Mono (mono), IBM Plex Sans (body). The prototype loaded them
 * from the Google Fonts CDN; we self-host the woff2 (committed under
 * ./fonts) via `next/font/local` so the static export has NO external
 * font origin at runtime and survives behind the localhost-only sidecar.
 *
 * Each family exposes a CSS variable consumed by the token layer in
 * globals.css (`--f-display` / `--f-mono` / `--f-body`). Only the display
 * face — a single variable-weight file and the heading/LCP face — is
 * preloaded; body and mono swap in to keep the critical request count low.
 */

// Space Grotesk — variable weight axis (covers 400–700 used by the handoff).
export const fontDisplay = localFont({
  src: "./fonts/space-grotesk-latin-wght.woff2",
  weight: "400 700",
  style: "normal",
  display: "swap",
  variable: "--f-display",
  preload: true,
  fallback: ["system-ui", "sans-serif"],
});

// JetBrains Mono — variable weight axis (covers 400–800 used by the handoff).
export const fontMono = localFont({
  src: "./fonts/jetbrains-mono-latin-wght.woff2",
  weight: "400 800",
  style: "normal",
  display: "swap",
  variable: "--f-mono",
  preload: false,
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
});

// IBM Plex Sans — static body face; 400 for copy, 600 for emphasis.
export const fontBody = localFont({
  src: [
    { path: "./fonts/ibm-plex-sans-latin-400.woff2", weight: "400", style: "normal" },
    { path: "./fonts/ibm-plex-sans-latin-600.woff2", weight: "600", style: "normal" },
  ],
  display: "swap",
  variable: "--f-body",
  preload: false,
  fallback: ["system-ui", "sans-serif"],
});

/** Combined `variable` class names to apply on the document root. */
export const fontVariables = `${fontDisplay.variable} ${fontMono.variable} ${fontBody.variable}`;
