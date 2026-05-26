import type { Metadata } from "next";
import type { ReactNode } from "react";
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

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
