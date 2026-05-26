// Audit Viewer surface — full design lands in E-7 (D4). This stub
// renders the route skeleton so D1 can ship the routing + sidecar
// plumbing without the design pass blocking it.

export default function AuditPage() {
  return (
    <main className="min-h-screen px-rhythm-wide py-section">
      <h1 className="font-display text-display">Audit Viewer</h1>
      <p className="mt-rhythm-base text-(--text-muted)">
        Implementation lands in E-7 (D4). See{" "}
        <code className="font-mono">docs/internal/v0.4_ui_rfc.md</code> §5.3.
      </p>
    </main>
  );
}
