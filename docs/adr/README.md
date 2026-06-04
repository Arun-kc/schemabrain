# Architecture Decision Records

Each ADR captures one significant, hard-to-reverse decision — the context, the
choice, and its consequences — at the moment it was made.

**ADRs are point-in-time records.** Once accepted, an ADR's rationale is not
rewritten when the decision later changes. Instead, a **new ADR supersedes** the
old one, and the old record stays in place (marked `Superseded by …`) as the
history of *why* the earlier call was made. Only stale code *pointers* (a renamed
function, a moved file) are corrected in place; the reasoning is left intact.

## Index

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-audit-row-and-pii-taxonomy.md) | Audit row shape + the sensitivity / PII category taxonomy | Accepted |
| [0002](0002-store-protocol-seam.md) | Store Protocol seam (data-access abstraction) | Accepted |
| [0003](0003-versioning-policy.md) | Versioning policy (pre-1.0 semver + schema versions) | Accepted |
| [0004](0004-observability-event-bus.md) | Observability event bus | Accepted |
| [0005](0005-dashboard-routing-under-static-export.md) | Dashboard routing under static export (read-only, localhost, no-Node) | Accepted |
| [0006](0006-policy-apply-under-read-only-sidecar.md) | Policy editing under the read-only sidecar invariant (Apply = copy YAML + reveal command) | Accepted |
| [0007](0007-policy-editor-control-model.md) | Policy editor control model — two levers (category block + per-column override) | Superseded by [0008](0008-policy-editor-handoff-verb-mapping.md) |
| [0008](0008-policy-editor-handoff-verb-mapping.md) | Policy editor — handoff-exact per-column `block/redact/allow` grid as a client projection | Accepted |
| [0009](0009-trust-surface-confidence-data-contract.md) | Trust-surface confidence data contract — cell-confidence (source), agent-quote (drop), refusal-confidence (drop) | Accepted |

## By area

**Dashboard & Policy editor.** Read together for how the editable Policy surface
works:

- [0005](0005-dashboard-routing-under-static-export.md) — the read-only,
  localhost, static-export serving contract the dashboard lives under.
- [0006](0006-policy-apply-under-read-only-sidecar.md) — how "Apply" works
  without a write route (the GET-only invariant; `Apply` copies canonical YAML +
  reveals `schemabrain policy apply`). Orthogonal to the control model — it held
  unchanged across the 0007 → 0008 redesign.
- [0008](0008-policy-editor-handoff-verb-mapping.md) — the current control model:
  the per-column 3-way grid as a projection over the engine's
  category-block × column-override state. ([0007](0007-policy-editor-control-model.md)
  is the superseded two-lever predecessor, kept for history.)

**Engine & data model.**

- [0001](0001-audit-row-and-pii-taxonomy.md) — the PII taxonomy + catastrophic
  floor the Policy editor operates on; the audit-row shape.
- [0002](0002-store-protocol-seam.md) — the Store Protocol the sidecar reads
  through.
- [0004](0004-observability-event-bus.md) — the event bus.
- [0009](0009-trust-surface-confidence-data-contract.md) — the per-column PII
  confidence band the matrix renders (deterministic, index-time, advisory), and
  why the Refusals quote + confidence meter are dropped rather than fabricated.

**Project policy.**

- [0003](0003-versioning-policy.md) — versioning + schema-version bump rules.

## Adding an ADR

Number sequentially (`NNNN-kebab-title.md`), use the frontmatter + section shape
of an existing record (`Context` / `Decision` / `Consequences` / `References`),
and set `Status: Accepted`. To change a prior decision, add a new ADR that
`Supersedes` it and mark the old one `Superseded by …` — do not edit the old
decision in place.
