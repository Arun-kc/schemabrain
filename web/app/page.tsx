import Link from "next/link";

/**
 * Dashboard landing surface.
 *
 * Renders three entry cards — one per M1 surface (PII Visualization,
 * Refusal Experience, Audit Viewer). The real surface designs land
 * in E-5 / E-6 / E-7 (D2-D4); this page proves the routing skeleton
 * works end-to-end before the surfaces ship.
 */
export default function HomePage() {
  return (
    <main className="min-h-screen bg-(--surface-base) px-rhythm-wide py-section">
      <header className="mx-auto max-w-5xl mb-section">
        <p className="font-mono text-sm uppercase tracking-widest text-(--text-muted)">
          SchemaBrain · v0.4 · dashboard
        </p>
        <h1 className="font-display text-display leading-[1.05] text-(--text-primary) mt-rhythm-base">
          What the SQL firewall sees.
        </h1>
        <p className="mt-rhythm-base max-w-2xl text-(--text-secondary)">
          Read-only views into the entities indexed, the refusals issued,
          and the tamper-evident audit chain that records every MCP tool
          call.
        </p>
      </header>

      <section className="mx-auto max-w-5xl grid grid-cols-1 gap-rhythm-base md:grid-cols-3">
        <SurfaceCard
          href="/pii"
          eyebrow="01 — Surface"
          title="PII Visualization"
          body="Which columns carry catastrophic-leak categories — and which entities would get refused."
        />
        <SurfaceCard
          href="/refusals"
          eyebrow="02 — Surface"
          title="Refusal Experience"
          body="Live feed of refused tool calls with the envelope an agent actually saw."
        />
        <SurfaceCard
          href="/audit"
          eyebrow="03 — Surface"
          title="Audit Viewer"
          body="Browse the mcp_audit chain. Verify it hasn't been tampered with."
        />
      </section>
    </main>
  );
}

function SurfaceCard({
  href,
  eyebrow,
  title,
  body,
}: {
  href: string;
  eyebrow: string;
  title: string;
  body: string;
}) {
  return (
    <Link
      href={href}
      className="group block border border-(--border-subtle) bg-(--surface-raised) p-rhythm-wide transition-transform duration-normal ease-out-expo hover:-translate-y-0.5 hover:border-(--border-emphasis)"
    >
      <p className="font-mono text-xs uppercase tracking-widest text-(--text-muted)">
        {eyebrow}
      </p>
      <h2 className="mt-rhythm-tight font-display text-2xl text-(--text-primary)">
        {title}
      </h2>
      <p className="mt-rhythm-base text-(--text-secondary)">{body}</p>
      <p className="mt-rhythm-wide font-mono text-xs text-(--color-signal-green) opacity-0 transition-opacity duration-fast group-hover:opacity-100">
        open →
      </p>
    </Link>
  );
}
