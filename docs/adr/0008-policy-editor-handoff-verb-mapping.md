---
title: "ADR 0008 — Policy editor: handoff-exact per-column verb mapping"
description: "The Policy editor matches the design handoff exactly — a single per-column block/redact/allow grid — and maps those three verbs onto the engine's category-block + column-override model: block adds the column's categories to the block set (category-wide, disclosed), allow writes a categories:[] override, redact is classified-but-not-blocked. Supersedes ADR 0007's two-lever split."
---

# ADR 0008 — Policy editor: handoff-exact per-column verb mapping

- **Status:** Accepted
- **Date:** 2026-06-03
- **Supersedes:** ADR 0007 (control model)

## Context

ADR 0007 chose to split the design handoff's single per-column
`block / redact / allow` grid into two honest levers (a category block
panel + a per-column `mark safe` override list), because the engine blocks
by *category*, not per-column, and has no operator-selectable per-column
"redact". After seeing the built surface against the handoff, the operator
chose **handoff-exact fidelity**: the Policy page must match the handoff's
single per-column grid with the literal 3-way control.

The engine facts from ADR 0007 still hold — enforcement is category-grained
and binary; the canonical policy is `block:` (categories) +
`column_overrides:` (per-column reclassification); the floor
(`credential` / `payment_card` / `government_id`) is always-on. So the
per-column grid cannot be a faithful 1:1 of a per-column data model; it must
be a **client projection** over the real (block-set × overrides) state,
with the verbs mapped honestly and their category-wide effects disclosed
rather than hidden. This ADR locks that mapping.

## Decision

### 1. The grid is handoff-exact; the per-column verb is a projection.

The Policy page renders the handoff layout verbatim: one
`Per-column enforcement` pane listing every tagged column, each with a 3-way
`block | redact | allow` segmented control (floor columns render
`floor · locked` with no control), beside the server-rendered
`schemabrain.yaml` panel and the staged diff. There is **no** separate
category panel.

A column's displayed verb is *derived* from the staged state
(`lib/policy.ts#columnVerb`), and a verb click *mutates* the staged
(block-set, overrides) pair (`applyVerb`). The staged pair — not a
per-column verb map — is the source of truth and what the read-only
`GET /api/pii/policy/preview` route renders (so the backend is unchanged
from ADR 0006).

### 2. Verb → engine mapping.

For a non-floor column with base (classifier/operator) categories `cats`:

| Verb | Staged mutation | Meaning | YAML effect |
| --- | --- | --- | --- |
| **block** | `block ∪= cats`; drop override | refused at `get_metric` (+ describe) | `cats` appear under `block:` |
| **redact** | `block ∖= cats`; drop override | classified but not blocked — aggregates allowed; the floor still redacts descriptions for any catastrophic tag | `cats` absent from `block:`; no override |
| **allow** | add `{sensitivity: internal, categories: []}` override; leave `block:` untouched | operator asserts the column non-sensitive (the `card_number_last4` fix) — exempt even if its category stays blocked for siblings | a `column_overrides:` entry |

Derivation (`columnVerb`): effective categories = the override's categories
if one is staged, else `cats`. If any effective category is in the block set
→ **block**; else if a `categories: []` override is staged → **allow**; else
→ **redact**.

### 3. `block` and `redact` are category-wide — and say so.

Because blocking is category-grained, `block`/`redact` on one column also
flips every sibling column sharing those categories. This is **disclosed in
the UI**, not hidden: a blocked row whose category is shared shows an inline
note ("category-wide: also blocks N other column(s)"), computed by
`siblingsAffectedByVerb`. The honest framing is *"block is a property of
the category; allow is a property of the column."* `allow` is the only
genuinely per-column verb (it writes a per-column override), which is why the
canonical false-positive workflow (`card_number_last4`) maps to it.

### 4. `redact` is a real, distinct policy state — with a stated caveat.

`redact` (classified, not in the block set) is a real configuration
distinct from both `block` (refused) and `allow` (asserted non-sensitive):
the column stays labelled PII, so it appears as sensitive in the matrix and
is caught the moment its category is later blocked. The honest caveat,
documented in tooltip copy: for a **non-floor** column, `redact` and `allow`
permit the same aggregate-`get_metric` access today — they differ in
classification, not in the aggregate gate. We do not imply per-column
value-level redaction the engine doesn't perform.

### 5. The floor stays locked; no downgrade path in the UI.

Floor columns (any catastrophic base category) render `floor · locked` with
no control. The grid never offers — and the staged YAML never produces — a
downgrade of a floor column. `--force-catastrophic-downgrade` stays CLI-only.

### 6. The YAML panel renders the real schema, syntax-highlighted.

The handoff's YAML mock uses a fictional `pii_floor:` + `columns: {col:
verb}` schema. The panel instead renders the **canonical**
`version` / `block:` / `column_overrides:` body via `policy_to_yaml`
(server-side, ADR 0006) so the copied YAML is byte-exact for
`schemabrain policy apply`. It carries the handoff's *visual* treatment
(syntax-highlighted keys/values/comments, with catastrophic-floor category
values in the alarm color via `lib/policy.ts#highlightYaml`). This is the
one deliberate content divergence from the handoff mock: the look matches,
the bytes are real.

## Consequences

**What becomes possible**

- Pixel-faithful parity with the handoff Policy design (single per-column
  3-way grid + syntax-highlighted YAML), while every control still drives
  the real, `policy apply`-able policy.
- The backend is untouched: the grid projects onto the existing
  `GET /api/pii/policy/preview` (block + overrides) contract.

**What constrains / what to watch**

- `block`/`redact` have category-wide reach by construction. The disclosure
  note is load-bearing — it must render whenever a sibling is affected, or
  the control misleads. (Verified in tests + e2e.)
- For non-floor columns, `redact` vs `allow` are aggregate-gate-equivalent;
  copy must not over-claim per-column redaction.
- The displayed verb is derived, so toggling one column can visibly change a
  sibling's segment. That is correct (it mirrors the engine) but is a UX
  consequence to keep legible.

**What remains deferred**

- In-dashboard category escalation to an arbitrary non-empty set (the rare
  `policy tag` case) stays CLI-only.
- True per-column blocking would require an engine change (out of scope).

## References

- `web/components/policy/PolicyEditor.tsx` — the handoff-exact grid.
- `web/lib/policy.ts` — `columnVerb` / `applyVerb` / `siblingsAffectedByVerb`
  / `highlightYaml` (the projection + highlighter, unit-tested).
- `schemabrain/pii/policy.py`, `policy_yaml.py`, `categories.py` — the
  category-block + override model the verbs map onto.
- `schemabrain/dashboard/sidecar.py` — `GET /api/pii/policy/preview`
  (unchanged; renders the staged block + overrides).
- ADR 0006 — read-only Apply (copy YAML + reveal command).
- ADR 0007 — the superseded two-lever control model (kept as the
  point-in-time record of why the split was first chosen).
