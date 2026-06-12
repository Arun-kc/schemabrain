---
title: "ADR 0011 — Graph projection enrichment: edge cardinality + node pii_level (3-state); refusal_count deferred"
description: "The graph surface (PR-17) needs richer node/edge signals than the PR-16 contract carried: the design handoff labels every edge with cardinality, shades nodes by a three-state PII tier, and badges per-node refusal counts. This ADR records three source-or-drop calls in the ADR 0009/0010 honesty spirit. (1) Edge cardinality ships as a SNAPSHOT column on graph_edges, copied from the canonical join but ONLY for declared FK edges — log-mined/inferred edges stay null so an unverified shape is never rendered as engine-derived. (2) Node pii_level ships as the full 5-state value recomputed LIVE in the route, replacing the catastrophic boolean to keep one source of truth and inherit the live-floor-never-disagrees-with-/pii guarantee. (3) Per-node refusal_count is DEFERRED: mcp_audit carries no entity reference, so any number today would be fabrication; the future shape (a persisted-but-not-canonical anchor_entity column) is recorded but kept off the PR-17 critical path."
---

# ADR 0011 — Graph projection enrichment (cardinality + pii_level; refusal_count deferred)

- **Status:** Accepted
- **Date:** 2026-06-05
- **Extends:** [ADR 0010](0010-graph-projection-backend.md) (the PR-16 backend). ADR 0010 stands as written (it is a point-in-time record); this ADR adds to the `/api/graph` contract rather than rewriting it.

## Context

The marketed-vision launch makes the knowledge graph the signature surface
(`docs/internal/marketed_vision_launch_plan_2026_06_01.md`). PR-16 (ADR 0010)
shipped the backend with a deliberately small node/edge shape: node = `group`
+ live `catastrophic` boolean + `row_count`; edge = honest `evidence` band +
`canonical_path_rank`.

The design handoff (`design_handoff_schemabrain/app/graph.jsx`) renders three
signals the PR-16 contract does not carry:

1. a **cardinality** label on each relationship edge (`N:1` / `1:1`);
2. a **three-state PII tier** per node (no PII / has PII / catastrophic), a
   lighter halo for the middle tier vs the catastrophic alarm;
3. a **per-node refusal-count** badge.

A code-grounded feasibility pass found these three are NOT equally derivable.
Shipping them naively would either fabricate data (a cardinal sin for a
trust/safety product) or split the catastrophic signal across two sources of
truth. This ADR records the calls so PR-17 codes against the final contract.

## Decision

### 1. Edge `cardinality` — SNAPSHOT, declared-FK edges only

`canonical_joins.cardinality` already persists the equi-join shape
(`one_to_one` / `one_to_many` / `many_to_one` / `many_to_many`), inferred at
join-suggest time from FK-column-set vs PK-column-set membership
(`joins/suggest.py::_infer_fk_cardinality`). The projection writer already
holds the join in hand, so the value is a **zero-cost field copy** onto a new
`graph_edges.cardinality` column (store schema **v16**, an additive read-model
ALTER repopulated by the next projection rebuild — no data migration).

It is a **snapshot**, not a live overlay (unlike the PII floor): cardinality is
a static property of the persisted join, so the snapshot can never drift the
way a PII tag can.

**Honesty gate (load-bearing):** the writer carries `cardinality` ONLY when the
edge is `declared` (FK-backed). A `log_mined` or `inferred` (manually-authored
/ LLM-suggested / dbt-imported) join MAY still carry an operator- or
dbt-asserted `cardinality` in its YAML, but the engine never validated it
against a live DB constraint. Rendering that identically to an FK-derived
value would dress a guess as fact. So `_graph_edge_from_join` drops it for
non-declared edges (`cardinality = join.cardinality if origin == "declared"
else None`). `null` on the wire means "not derivable from a declared
constraint" — never a guess.

**Documented conservative inaccuracy:** `_infer_fk_cardinality` uses
PK-membership as its only uniqueness proxy; the Postgres connector does not
read standalone `UNIQUE` constraints. A declared FK referencing a column
covered by a standalone `UNIQUE` (not the PK) is therefore classified
`many_to_one` when it is truly `one_to_one`. This is wrong-but-conservative —
it over-counts the "many" side, never invents a tighter shape than the
evidence supports. The field is "cardinality from the declared PK/FK shape,"
not "true cardinality"; surfaces must not market it as exact.

### 2. Node `pii_level` — LIVE, 5-state, replaces the `catastrophic` boolean

The route already computes `_entity_pii_level(...)` per node (the SAME helper
the PII matrix uses) and then **collapses it** to `== "catastrophic"`,
discarding four of five states. The enrichment simply stops discarding it: the
node emits the full `pii_level` (`catastrophic` / `pii` / `confidential` /
`internal` / `none`). No store change, no migration — PII is a pure live
overlay; `graph_nodes` stores nothing about PII.

`catastrophic: boolean` is **replaced**, not supplemented. Keeping both would
create two sources of truth for `catastrophic === (pii_level ===
'catastrophic')` — exactly the drift the live overlay exists to prevent. The
surface reads `pii_level === 'catastrophic'` for the alarm and the
non-catastrophic tiers for the lighter halo, from one value. The TS type reuses
the existing `PiiLevel` (`web/lib/types/meta.ts`), already emitted by
`/api/entities`.

**Middle-tier wording rule (binds PR-17):** `confidential` and `internal` are
*sensitivity* levels, not PII *categories*. A halo labelled "has PII" that
lights up on a `confidential`/`internal`-only entity would overstate PII
presence. So:

- if the middle-tier halo label reads **"has PII"**, map ONLY `pii` → middle;
- if it maps `pii` / `confidential` / `internal` → middle, the label MUST read
  **"sensitive (non-catastrophic)"**.

The backend value is honest (a real classifier tag, category/sensitivity-derived
— advisory confidence bands never gate it, per ADR 0009). The frontend must gate
on the `pii_level` **string** and never re-derive catastrophic-ness from a
chip-kind helper client-side.

### 3. Per-node `refusal_count` — DEFERRED (not derivable today)

`mcp_audit` carries no table/column/entity reference (`audit/ddl.py`): the
entity identity exists in-process at refusal time
(`PiiBlockedError.anchor_entity`, `mcp/server.py`) but `build_audit_row`
discards it by privacy-by-construction. There is no column to `GROUP BY`, so
**any per-node count today would be fabrication.** It is dropped from PR-17 and
the per-node refusal badge is cut from the first surface ship.

The future shape, recorded so it is not re-litigated: add a nullable,
**persisted-but-NOT-canonical** `mcp_audit.anchor_entity` column (so the
RFC-6962 chain/Merkle hash over `canonical_audit_row` is unaffected and
historical verification still passes), populate it from the in-process anchor,
and aggregate `refusal_count` LIVE in the route (like the PII floor — the audit
log is an append-only growing stream a snapshot would lag). When pursued, a
bare `0` is honest as "zero **attributable** refusals" ONLY if the column
exists AND an explicit "unattributed refusals" aggregate is surfaced
(non-entity-scoped refusals — `cost_cap_exceeded`, `schema_drift`,
`ambiguous_resolution`, `allowlist_violation`, and `pii_blocked` with a null
anchor — carry no entity and would otherwise make the node-sum silently
undershoot the total). This touches the tamper-evident audit log — the most
sensitive surface in the product — so it is its own PR, kept off the graph
surface's critical path.

## Consequences

- `/api/graph` edges gain `cardinality` (declared-only; null otherwise); nodes
  swap `catastrophic` for the 5-state `pii_level`. Dashboard wire contract bumps
  `DASHBOARD_SCHEMA_VERSION` 1.6 → 1.7; the `Cardinality` enum is now parity-
  checked TS↔Python by `test_ts_charter_sync.py`.
- Store schema bumps **v15 → v16** for the single additive `graph_edges.cardinality`
  column. `_migrate_v15_to_v16` ALTERs an existing v15 store; a store chaining up
  from v13/v14 gets the column from the fresh DDL instead (the ALTER is guarded on
  table existence because the v15 graph tables are created by the DDL loop, which
  runs after migrations). No data migration — a reindex/reapply repopulates it.
- The shipped graph is **honestly leaner** than the handoff prototype: no edge
  cardinality on mined/inferred edges, no per-node refusal badge. The surface
  narrates the gaps as roadmap rather than rendering fabricated data.
- Smoke note: any pre-existing v15 projection (e.g. the stashed live SaaS store)
  reads `cardinality = null` until the projection is **rebuilt** — a manual smoke
  must reindex/reapply, not just hit the route against a stale projection.

## Alternatives considered

- **Keep `catastrophic` AND add `pii_level`** — rejected: two sources of truth
  for the same fact, the exact drift ADR 0010's live overlay exists to prevent.
- **Carry cardinality on every edge (including mined/inferred)** — rejected:
  unverified shapes rendered as engine-derived fact; the honesty line in §1.
- **Ship `refusal_count` via a `tool_name`/`pii_categories` join** — rejected:
  those columns do not identify an entity; the result would be fabricated.
- **Block PR-17 on the `mcp_audit.anchor_entity` work** — rejected: it touches
  the tamper-evident audit log and is independently PR-sized; ship the graph
  without the badge and fast-follow.
