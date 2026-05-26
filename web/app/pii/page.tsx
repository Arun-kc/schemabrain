import { Matrix } from "@/components/pii/Matrix";

/**
 * PII Visualization surface — "The Ledger" (Direction A from
 * docs/internal/v0.4_pii_viz_design_pass.md).
 *
 * Server Component shell hosts the Matrix client component, which
 * owns selection state + queries. URL state for selected entity is
 * deferred to v0.5 per design doc §"Tradeoffs".
 */
export default function PiiPage() {
  return (
    <main className="min-h-screen bg-(--surface-base)">
      <Matrix />
    </main>
  );
}
