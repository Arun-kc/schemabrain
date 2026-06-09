"use client";

import { useEffect } from "react";

/**
 * Dashboard root redirect.
 *
 * The dashboard's home is `/overview`. `/` exists only as the static-export
 * entry (`index.html`) and immediately forwards there — in the wheel the
 * sidecar serves this stub at `/`, and `next dev` forwards too. The marketing
 * landing that used to live here now ships as the separate `site/` app
 * (deployed to Vercel), out of the pip wheel. See
 * docs/internal/landing_site_separation_plan_2026_06_09.md.
 */
export default function RootRedirect() {
  useEffect(() => {
    window.location.replace("/overview");
  }, []);

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        fontFamily: "var(--font-mono, ui-monospace, monospace)",
        fontSize: 13,
        color: "var(--text-muted, #888)",
      }}
    >
      {/* No-JS / pre-hydration fallback: forward without the React effect. */}
      <meta httpEquiv="refresh" content="0; url=/overview" />
      <p>
        Redirecting to <a href="/overview">/overview</a>…
      </p>
    </main>
  );
}
