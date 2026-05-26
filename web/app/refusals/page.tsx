// Refusal Experience UI surface — full design lands in E-6 (D3),
// iteration v2 in E-9 (D5). This stub renders the route skeleton so
// D1 can ship the routing + sidecar plumbing without the design pass
// blocking it.

export default function RefusalsPage() {
  return (
    <main className="min-h-screen px-rhythm-wide py-section">
      <h1 className="font-display text-display">Refusal Experience</h1>
      <p className="mt-rhythm-base text-(--text-muted)">
        Implementation lands in E-6 (D3) + E-9 (D5). See{" "}
        <code className="font-mono">docs/internal/v0.4_ui_rfc.md</code> §5.2.
      </p>
    </main>
  );
}
