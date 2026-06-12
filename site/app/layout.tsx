import type { Metadata } from "next";
import type { ReactNode } from "react";
import { fontVariables } from "./fonts";
import { TAGLINE } from "@/lib/positioning";
import "./globals.css";

export const metadata: Metadata = {
  title: "SchemaBrain — the trust and intelligence layer for AI agents",
  description: TAGLINE,
};

// Runs before React hydrates: apply the persisted theme to <html> so there is
// no flash. The marketing site is DARK-first ("Observatory") — light ("Atlas")
// only when the visitor explicitly chose it. The dark tokens key off
// `[data-theme="dark"] .sb-app`, so the attribute lives on <html>.
const THEME_INIT_SCRIPT = `
try {
  var t = localStorage.getItem("schemabrain.theme");
  if (t !== "light") document.documentElement.setAttribute("data-theme", "dark");
} catch (_) { document.documentElement.setAttribute("data-theme", "dark"); }
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={fontVariables} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
