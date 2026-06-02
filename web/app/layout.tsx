import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "./providers";
import { fontVariables } from "./fonts";
import "./globals.css";

export const metadata: Metadata = {
  title: "SchemaBrain Dashboard",
  description:
    "Read-only dashboard for the SQL firewall between AI agents and your database.",
  // The sidecar serves this app at 127.0.0.1; no public indexing
  // should ever happen. Belt-and-suspenders meta tag in addition to
  // the bind-localhost contract.
  robots: { index: false, follow: false },
};

// Inline script that runs before React hydrates, reading the
// persisted theme and applying it to <html> so there's no flash
// between SSR's default-light and the user's preferred dark.
const THEME_INIT_SCRIPT = `
try {
  var t = localStorage.getItem("schemabrain.theme");
  if (t === "dark") document.documentElement.setAttribute("data-theme", "dark");
} catch (_) {}
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // The inline THEME_INIT_SCRIPT below mutates data-theme on <html>
    // before React hydrates, so the attribute intentionally differs
    // from SSR output. suppressHydrationWarning scopes the suppression
    // to this element's attributes only (one level deep) — children
    // still hydrate normally.
    <html lang="en" className={fontVariables} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
