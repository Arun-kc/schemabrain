---
title: "ADR 0009 — Trust-surface confidence data contract (cell-confidence / agent-quote / refusal-confidence)"
description: "The three Trust-surface reskins (PII matrix, Refusals, Audit) each wanted a data field the engine did not yet produce honestly. This ADR resolves each as source-or-drop: cell-confidence gets a real source — a deterministic index-time per-column PII band derived from name + value-shape signal; agent-quote and refusal-confidence are dropped because the honest source does not exist. It also fixes the three distinct confidence axes (entity-binding vs PII-classification vs refusal) so they are never conflated."
---

# ADR 0009 — Trust-surface confidence data contract

- **Status:** Accepted
- **Date:** 2026-06-04

## Context

The marketed-vision launch reskins three existing Trust surfaces — the PII
matrix (→ a column × category confidence heatmap), Refusals (→ a
protective-framed timeline), and Audit (→ a hash-chain verifier). Each
target design references a data field the engine did **not** already
produce:

1. **Cell-confidence.** The heatmap wants a per-cell intensity. Today
   `GET /api/entities/pii-matrix` returns integer **counts** per (entity ×
   category) — there is no 0..1 or banded confidence per column
   (`dashboard/sidecar.py`).
2. **Agent-quote.** The refusals timeline mockup shows the offending
   query. The audit row (`mcp_audit`, `audit/ddl.py`) stores **no request
   text** — privacy-by-construction (ADR 0001): the fingerprint excludes
   row content, column names, literal SQL, caller/source identity, and
   timestamps, and the field count is CI-pinned.
3. **Refusal-confidence.** The mockup shows a per-event confidence meter.
   No such value exists; `_serialize_refusal` returns `confidence: None`.

The launch plan's honesty guardrails forbid fabricating any of these
(no faked confidence meters, no reconstructed quotes, render `—` / hide
rather than invent). So before any reskin, each field must be resolved
**source-or-drop**: either name a real, honest source the surface can
render, or drop the field from the design.

A second hazard the critic flagged: the codebase already has an entity
`bind_confidence` (v15) that is a *different* thing from a PII-cell
confidence. Shipping a second "confidence" without nailing the
distinction invites conflation in code, API field names, and UI copy.

## Decision

### 1. Cell-confidence — SOURCE (real, deterministic, index-time).

The heatmap's source is a per-column **`pii_confidence` band** —
`floor_locked | high | medium | low` — persisted on `column_pii_tags`
(the column already exists, added by v15; this ADR's PR is the
"index-time score writer lands in a follow-up" the schema comment
promised). It is computed at index time by `pii/confidence.py`
(`classify_pii_confidence`) from two signals already collected during
profiling:

- the **name signal** — the categories the name-regex classifier matched
  (`classify_column`'s output); a match is real evidence; and
- the **value-shape signal** — `ColumnStats.shape_patterns`, structural
  signatures (`shape_of`: digit→`9`, lower→`a`, upper→`A`, separators
  kept) of the most common values, **computed from raw in-memory values
  at index time in the profiler — not from redacted samples**.

Banding (v1):

- Any catastrophic-floor category (`credential` / `payment_card` /
  `government_id`) → **`floor_locked`**, score `NULL`. The tag is
  always-on enforced, so confidence is moot — the band communicates
  "locked," not a probability.
- Otherwise the score starts at a name-match base (`0.6`) and gains a
  corroboration bonus (`+0.3`) when a value shape positively matches one
  of the matched categories' characteristic shape families
  (`contact` → email/phone, `location` → coordinate, `online_identifier`
  → IPv4/UUID). `score ≥ 0.8 → high`, else `medium`.
- v1 deliberately **never emits `low`.** Producing a low-confidence PII
  claim from a name match we cannot positively *refute* would under-state
  real sensitivity — the dangerous direction. `low` stays reserved for a
  future operator assertion or a richer heuristic with a genuine
  refutation signal.

Three properties make this honest rather than theater:

- **Deterministic.** No LLM, no calibrated probability. A `0.87` from a
  generation model is theater; this is reproducible from stored inputs.
- **Advisory, never an enforcement gate.** Enforcement is category
  membership, decided in the compiler / `get_metric` path. A `medium`
  band never weakens a block, and a floor column is blocked regardless of
  band. The matrix is a *view*.
- **No fabrication on absence.** The score is computed from raw shapes
  that are gone post-profile, so it cannot be backfilled. Pre-spike
  stores, SQLite sources with no shapes, and public columns all read back
  `(None, None)` and render `—` — never a faked `0` or a default band.

The raw `pii_confidence_score` (REAL) is the internal source of truth;
only the **band** is surfaced on the API. Per the launch plan's
confidence-representation lock (categorical, no numeric %), the score is
never sent to the UI.

`GET /api/entities/pii-matrix` gains an additive `columns[]` projection
(one entry per entity-bound column: `entity`, `qualified_table`,
`column_name`, `sensitivity`, `categories` in canonical order,
`pii_confidence` band-or-null). The existing entity-aggregated `counts`
shape is unchanged. `DASHBOARD_SCHEMA_VERSION` → `1.4`.

### 2. Agent-quote — DROP.

The refusals timeline shows **no reconstructed query**. Persisting
request text, normalized SQL, or row content would violate the ADR 0001
privacy-by-construction invariant (the audit fingerprint has no
string-typed SQL field by construction; the exclusion is type-enforced
and CI-pinned). No new free-text request column is added — doing so would
require explicit privacy + schema sign-off and is out of scope for a
reskin. The timeline conveys *what* and *why* from fields that already
exist honestly: `refusal_reason` (the taxonomy below), the blocked
`pii_categories`, `tool_name`, `occurred_at`, and the `fingerprint` /
`chain_hash` for the audit surface. The reachable refusal *envelope*
(the charter error object) stays the deepest detail offered.

### 3. Refusal-confidence — DROP.

A policy refusal is a **deterministic decision**: a category is in the
blocked set, or it is not. There is no probabilistic inference and
therefore no honest confidence to show — a meter would be invented
precision. The timeline drops the confidence meter. Refusal *reasons* are
the real signal; the six-value taxonomy
(`pii_blocked`, `allowlist_violation`, `fragment_unsafe`,
`cost_cap_exceeded`, `ambiguous_resolution`, `schema_drift`) collapses
into the three buckets the timeline badges (only `pii_blocked` is emitted
today; the other five are reserved and land with their enforcement
paths).

### 4. The three confidence axes are distinct and never conflated.

| Axis | Column | Type | Rates | Produced |
| --- | --- | --- | --- | --- |
| Entity-binding | `entities.bind_confidence` | TEXT `high/medium/low` | "Is this table really this entity?" (binding correctness) | LLM self-rating on `entities suggest --apply`; store-only, resets to NULL on YAML round-trip |
| PII-classification | `column_pii_tags.pii_confidence` | TEXT `floor_locked/high/medium/low` | "Does the data back this column's PII category?" (classification correctness) | Deterministic, index-time, name + value-shape (this ADR) |
| Refusal | — | — | "Was this refusal correct?" | **Dropped** — deterministic decision, no confidence exists |

They are orthogonal: a high-confidence entity binding can carry
uncertain PII classifications, and a well-classified column says nothing
about the binding. The API field names keep them apart — entity routes
expose `confidence` (the binding self-rating); the matrix exposes
`pii_confidence` (the classification band). The dropped refusal axis has
no field at all.

## Consequences

- **PII matrix port (PR-15) has a real, no-NaN source.** It renders
  `pii_confidence` band intensity per cell, `floor_locked` as the
  alarm/lock treatment, and `—` for unscored columns. Pure-frontend, as
  planned.
- **A new persisted signal exists.** `pii_confidence` is written on every
  re-index. Changing the v1 banding rule later **re-labels** columns on
  the next index — acceptable because the band is advisory and
  non-gating, but a deliberate event (the heuristic lives in one pure
  module, `pii/confidence.py`, with a pinned band enum).
- **No migration.** The columns already exist (v15); this PR only adds the
  writer + reader surfacing. Old stores read back `NULL` → `—`.
- **Refusals/Audit reskins lose two mockup elements** (the quote and the
  meter) by design. The protective-framing they need comes from reasons +
  blocked categories + the verifiable hash chain, all of which exist.
- **Floor categories stay always-on.** `floor_locked` is a display
  consequence of the existing catastrophic floor, not a new control.

## References

- ADR 0001 — audit row shape + PII taxonomy + the privacy-by-construction
  invariant that forces the agent-quote drop.
- `schemabrain/pii/confidence.py` — the deterministic banding heuristic.
- `schemabrain/pii/categories.py` — `PiiConfidenceBand` / catastrophic
  floor.
- `schemabrain/core/store.py` — `write_column_pii_tags(confidence=…)` +
  `get_column_pii_confidence`; the `pii_confidence` SQL CHECK.
- `schemabrain/dashboard/sidecar.py` — the `columns[]` projection on
  `/api/entities/pii-matrix`.
- The marketed-vision launch plan — `wsTRUST-data-contract-spike`.
