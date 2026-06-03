---
title: "ADR 0007 — Policy editor control model (category block + per-column overrides)"
description: "The dashboard Policy editor exposes the engine's two real levers as two distinct controls — a category-level block panel (the floor locked) and a per-column override list — instead of the handoff's single per-column block/redact/allow grid, because enforcement is category-grained and binary and there is no operator-selectable per-column 'redact'."
---

# ADR 0007 — Policy editor control model

- **Status:** Accepted
- **Date:** 2026-06-03

## Context

The design handoff (`app/policy.jsx`) renders the Policy editor as a single
per-column grid where each column carries a **3-way
segmented control: block / redact / allow**, plus an invented YAML schema
(`pii_floor:` + `columns: {table.col: verb}`). It is a high-fidelity *visual*
reference, but its control model does not match the SchemaBrain enforcement
engine. The engine's ground truth (`schemabrain/pii/policy.py`,
`schemabrain/pii/categories.py`, `schemabrain/pii/policy_yaml.py`):

- **Enforcement is category-grained, not column-grained.** The policy's blocking
  lever is `block: frozenset[PIICategory]` — a set of *categories* the server
  refuses at `get_metric`. A column is refused iff one of *its* categories is in
  the effective block. There is no per-column block bit; "block this column" is
  not a primitive the engine has.
- **Enforcement at the read gate is binary.** A column's verdict is `allowed`,
  `floor_blocked`, or `blocked` — surfaced or refused. There is no third
  "redact" verdict an operator can select per column.
- **Redaction is the always-on catastrophic floor, not a choice.**
  `CATASTROPHIC_LEAK_CATEGORIES` (`credential` / `payment_card` /
  `government_id`) is unconditionally enforced: `describe_entity` always redacts
  a column's description when one of those categories appears, regardless of
  operator policy. It is a floor, not a per-column toggle.
- **The real per-column lever is reclassification, not a verdict toggle.**
  `ColumnOverride` lets an operator assert a column's `(sensitivity, categories)`,
  *replacing* the heuristic classifier's row. The canonical use is fixing a
  false positive — e.g. `card_number_last4`, which the name-heuristic flags
  `payment_card` but PCI-DSS Q&A says is not sensitive alone, so the operator
  asserts `sensitivity: internal, categories: []`. Lowering a column off a
  catastrophic category is refused (`CatastrophicDowngradeError`) without an
  explicit force flag.

So the policy has **two** real, distinct levers — a category-level block set and
per-column reclassification overrides — and the canonical YAML
(`schemabrain/pii/policy_yaml.py`) is exactly `version` + `block:` +
`column_overrides:`. The handoff conflates both into one per-column 3-way
control that maps cleanly onto *neither* and implies a per-column "redact"
capability the engine does not have.

This ADR decides how the editor's controls map onto those levers. The prior
read-only policy view (on `/pii`) already rendered the honest *derived* model
(active block, floor, category rollup, per-column verdict with provenance); this
ADR governs the *editable* surface that supersedes it on `/policy`.

## Decision

### 1. Two controls, one per real lever — not one per-column verb grid.

The editor splits the handoff's single grid into the two levers the engine
actually has, keeping the handoff's overall two-pane composition, YAML panel,
staged diff, drift banner, floor-lock affordance, and visual language:

- **Category block panel** → writes `block:`. The twelve `PIICategory` values as
  toggles. Toggling stages adding/removing a category from the block set. Each
  row shows its blast radius from the existing `category_rollup`
  (`column_count` / `entity_count` / sample columns) so "block `contact`" reads
  honestly as "refuse the N columns carrying contact data", not a single cell.
- **Per-column override list** → writes `column_overrides:`. The operator-
  overridable columns, each with a two-state control **`use classifier` ↔
  `mark safe`**. `mark safe` stages a `ColumnOverride(sensitivity=internal,
  categories=[])` — the false-positive fix. `use classifier` clears a staged or
  applied override back to the heuristic tag.

This is the minimum honest surface that covers both real operator workflows —
*tighten by category*, *fix a false positive by column* — without inventing a
control the engine can't honor.

### 2. The catastrophic floor is locked, in both controls.

The three floor categories render **on and locked** in the category panel
(lock + alarm affordance, reusing the PolicyView/handoff floor visual); they
cannot be toggled off. Any column carrying a floor category renders **`floor ·
locked`** in the override list with no actionable control. This is backed by the
engine: the floor is unconditional, and `CatastrophicDowngradeError` already
refuses a `mark safe` that would strip a catastrophic category. The UI never
offers — and the staged YAML never produces — a downgrade of a floor column; the
`--force-catastrophic-downgrade` escape hatch stays CLI-only and is deliberately
not surfaced in the dashboard.

### 3. Per-verb / per-lever YAML content is defined, and rendered server-side.

The staged state maps to canonical YAML deterministically:

| Operator action | Staged effect | Canonical YAML |
| --- | --- | --- |
| Category toggled **on** (non-floor) | add to block set | category appears under `block:` (floor categories always present) |
| Category toggled **off** | remove from block set | category absent from `block:` |
| Column **mark safe** | add `ColumnOverride(internal, [])` | `column_overrides: {<qualified_column>: {sensitivity: internal, categories: []}}` |
| Column **use classifier** | drop the override | qualified column absent from `column_overrides:` |
| Floor category / floor column | locked | always blocked; no override emitted |

The YAML panel and the diff are **rendered server-side** via the engine's own
`policy_to_yaml` through the read-only `GET /api/pii/policy/preview` route (ADR
0006): the client posts its staged block set + overrides as query params, the
sidecar builds a `Policy` and returns `policy_to_yaml(policy)` + the line diff
against the active policy. There is **no client-side YAML emitter** — the
dashboard cannot drift from the canonical grammar, because it never re-implements
it. The diff is expressed in YAML lines, matching the handoff.

### 4. The staged diff is the source of truth for "what will change".

Apply (governed by ADR 0006: copy YAML + reveal `schemabrain policy apply`) acts
on the server-rendered staged YAML. Because both panes derive from the same
preview response, the YAML the operator copies is byte-identical to what
`policy apply` will parse — the diff cannot lie about the outcome.

## Consequences

**What becomes possible:**

- A Policy editor whose controls are 1:1 with the engine's levers and with the
  on-disk `pii_policy.yaml` schema, so the surface teaches the correct mental
  model instead of papering over the category/column impedance mismatch.
- Both real workflows are first-class: category-level tightening (the primary
  blocking lever) and per-column false-positive correction (the override lever).
- Reuses the existing `GET /api/pii/policy` payload wholesale (active block,
  floor, category rollup, per-column with origin); only the small read-only
  `preview` route is new.

**What constrains future evolution:**

- **No per-column block bit.** Because blocking is category-grained, the editor
  cannot offer "block only this one column" — that is not a thing the engine
  does. If a future engine grows column-grained blocking, the panel can grow a
  per-column block control then, not before.
- **`mark safe` is the only per-column override verb in v1.** Asserting a
  *specific* non-empty category set on a column (the rarer escalation case) stays
  in `schemabrain policy tag` / hand-edited YAML; the dashboard surfaces that
  path in copy rather than shipping a per-column category picker. Revisit if
  operators ask for in-dashboard escalation.
- **The handoff's literal 3-way per-column control is not shipped.** The visual
  language, layout, YAML panel, diff, and floor lock are preserved; the
  block/redact/allow grid is replaced by the two honest levers above. This is a
  deliberate, documented divergence from the reference, per the handoff's own
  "recreate the UX, not verbatim" guidance.

**What remains deferred:**

- In-dashboard category escalation per column (assert arbitrary categories).
- Surfacing the `--force-catastrophic-downgrade` path in the UI — intentionally
  CLI-only; the dashboard never offers a floor downgrade.

## References

- `schemabrain/pii/policy.py` — `Policy`, `ColumnOverride`,
  `CatastrophicDowngradeError`.
- `schemabrain/pii/categories.py` — `PIICategory`, `SENSITIVITIES`,
  `CATASTROPHIC_LEAK_CATEGORIES`.
- `schemabrain/pii/policy_yaml.py` — `policy_to_yaml` / `parse_policy_yaml`, the
  canonical `version` + `block:` + `column_overrides:` grammar.
- `schemabrain/dashboard/sidecar.py` — `GET /api/pii/policy` (reused) and the new
  read-only `GET /api/pii/policy/preview`.
- `web/components/policy/PolicyEditor.tsx` — the editor this ADR governs; it
  supersedes the prior read-only PolicyView on `/policy`.
- the design handoff (`app/policy.jsx`) — the visual reference (layout, tokens,
  YAML panel, diff, drift banner) reconciled here.
- ADR 0006 — policy editing under the read-only sidecar invariant (how Apply
  works on the YAML this control produces).
- ADR 0001 — the sensitivity / PII category taxonomy these levers operate on.
