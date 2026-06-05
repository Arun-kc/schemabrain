---
title: "ADR 0010 — Knowledge-graph projection backend (populate + GET /api/graph)"
description: "PR-16 lands the backend half of the knowledge-graph surface: a persisted denormalised graph_nodes/graph_edges projection of entities + canonical joins, a public canonical-path builder that reuses the resolver's BFS with no metric dependency, and a read-only GET /api/graph. This ADR records the load-bearing decisions: where the projection is rebuilt (the semantic-write commands, NOT `index`-as-sole-hook), how the catastrophic floor stays consistent with the PII matrix (live overlay in the route), how the single canonical path is chosen honestly (longest deterministic shortest path), and the edge-evidence vocabulary (declared FK / log-mined / inferred — never inspected SQL)."
---

# ADR 0010 — Knowledge-graph projection backend

- **Status:** Accepted
- **Date:** 2026-06-05

## Context

The marketed-vision launch (`docs/internal/marketed_vision_launch_plan_2026_06_01.md`)
makes the knowledge graph the signature surface. PR-16 is its **backend
half**: `wsGRAPH-populate-on-index`, `wsGRAPH-api-route`,
`wsGRAPH-canonical-path-builder`, `wsGRAPH-web-types-and-fetch`. The
reactflow surface itself is PR-17.

The v15 migration already created two **empty** tables
(`graph_nodes`, `graph_edges`; `core/store.py`) in `_DDL_STATEMENTS` — a
denormalised read-model of `entities` + `canonical_joins`. No code
populates or reads them yet. PR-16 is the first writer + reader.

Four questions had no obvious answer and would be one-way doors if
guessed wrong:

1. **Where is the projection rebuilt?** The original plan said "at index
   time." But `schemabrain index` (`indexer.py:219`, `cli.py:_cmd_index`)
   indexes the *physical* schema (tables/columns/PII/embeddings/row-counts)
   and writes **no entities and no joins** — those come from `entities apply`,
   `joins apply`, `apply-project`, the init wizard, and dbt import. Hooking
   `index` alone would persist an empty graph.
2. **How does the catastrophic floor stay consistent with the PII matrix?**
   `graph_nodes.is_catastrophic` must never disagree with `/api/entities` /
   the PII matrix, which derive "catastrophic" live from
   `CATASTROPHIC_LEAK_CATEGORIES` (`pii/categories.py`). PII tags can change
   (`policy tag override`, re-`index`) without the graph's *structure*
   changing.
3. **What is "the" canonical path of an arbitrary schema?** The design
   handoff renders a hardcoded `order_item → order → user → tenant`. There is
   no honest way to fabricate a single spine by a fuzzy "find the root"
   heuristic.
4. **What vocabulary describes an edge's provenance?** The honesty charter
   (ADR 0001, launch §8) forbids any phrasing implying the engine inspects
   agent-authored SQL.

## Decision

### 1. The projection is a persisted read-model, rebuilt by the semantic-write commands.

`graph_nodes` / `graph_edges` stay a **persisted** projection (not
compute-on-read): the route reads a flat shape; it never re-walks the FK
graph per request. The projection is recomputed by an idempotent
`store.write_graph_projection(...)` (DELETE-by-source + `executemany`
INSERT in one `with conn:` transaction — mirrors `write_column_pii_tags`).

A single CLI helper `_refresh_graph_projection(store, source_id)` is
called at the **success path** of the commands that change a projection
input that is *served from the persisted shape*:

- `_cmd_index` — refreshes `row_count` + the at-rest catastrophic snapshot.
- `_cmd_entities_apply` — entity set / `group`.
- `_cmd_joins_apply` — edges.

`apply-project` is covered transitively (it delegates to
`_cmd_entities_apply` → `_cmd_joins_apply`).

**Deferred to a documented fast-follow (cause structural staleness only —
a new entity/join is not projected until the next `index`/`apply`, never a
floor inconsistency):** `entities suggest --apply`, `joins suggest --apply`,
`import dbt`, and the **init wizard** (the wizard's graph hand-off is owned
by `wsINIT-graph-payoff`, PR-22, which must add the rebuild there).

### 2. The catastrophic floor is a LIVE overlay in the route — never stale across surfaces.

The route computes each node's `catastrophic` boolean **live** from the
current column PII tags, via the same `_entity_pii_level` /
`CATASTROPHIC_LEAK_CATEGORIES` path the PII matrix and `/api/entities` use.
This makes the floor consistent with `/pii` **with zero staleness window**,
even after a bare `policy tag override` that no projection rebuild saw.

The persisted `graph_nodes.is_catastrophic` is still written by the
projection as the **at-rest read-model snapshot** (DDL coherence; consumed
by PR-17's server seed and any direct-table reader). The route's live
recompute is authoritative for the served response. This is deliberate
defence-in-depth on the one cross-surface invariant that must never break,
not a contradiction: a structural-only hook set cannot, by construction,
leave the served floor inconsistent with the matrix.

`row_count` and `group` are served from the persisted projection (refreshed
on `index`/`apply`); they are structural/cheap and carry no safety claim.

### 3. The single canonical path = the longest deterministic shortest path.

`canonical_path` is the **graph's longest shortest path** (its diameter):
honest ("the longest canonical-join chain in your schema"), deterministic,
and computed by reusing the resolver's BFS. For the SaaS demo this is
exactly `order_item → order → user → tenant`. Ties break lexicographically
on the entity-node-name sequence. Empty when the graph has < 2 connected
entities. The diameter scan is bounded by the entity count (the longest a
simple path can be), **not** the metric resolver's 6-hop default — a deep
schema's spine is never silently truncated.

- `build_canonical_path(*, joins, anchor, target)` is the public, reusable,
  **metric-free**, **pure** primitive (`semantic/compiler/resolve.py`,
  alongside the helpers it reuses). It takes an in-memory `list[CanonicalJoin]`
  (the caller resolves it from the store) and runs `_build_join_graph` →
  `_structural_shortest_paths` over it — it **never** touches a store and
  **never** routes through `resolve_metric_plan` / `store.get_metric`. Unlike
  the metric compiler it **never raises**: it returns an empty path when the
  endpoints are absent / equal / unreachable (the read-model has no `via=`
  disambiguation surface, so parallel canonicals on a hop collapse to the
  first-alphabetical name — mirroring `_render_structural_path_as_canonical_sequence`).
- `longest_canonical_path(*, joins)` picks the diameter endpoints
  deterministically and returns that path. `rebuild_graph_projection`
  (`semantic/graph_projection.py`) reads `store.list_canonical_joins` once and
  hands the list to it — the store read lives in the projection layer, not the
  pure builder.
- The projection writes `canonical_path_rank = 1` on the edges of the chosen
  path; all other edges keep the default `0`. Rank `2` (alternate paths) is
  reserved for PR-17 and intentionally **not** emitted yet (the `CHECK
  (0,1,2)` already permits it). The route reconstructs the ordered
  `canonical_path` from the **rank-1** edge set only (a trivial
  degree-1-endpoint walk — not a re-walk of the full graph); rank-2 edges are
  excluded so a future alternate never folds the primary path into a cycle.

### 4. Edge evidence vocabulary: declared FK / log-mined / inferred — never "inspected SQL".

`graph_edges.edge_origin` (`CHECK IN ('declared','log_mined','inferred')`)
is projected from `CanonicalJoin.inference_method`:

- `fk_constraint` → `declared`
- `observed_in_query_log` → `log_mined` (mirrors the existing
  `is_log_mined` test at `sidecar.py`)
- everything else (`manually_authored`, `llm_suggested`, `dbt_import`) →
  `inferred`

The wire/`GraphResponse` carries `evidence` verbatim as that closed union;
honest display labels are "declared FK" / "log-mined from query logs" /
"inferred". Never "inspected SQL" — the engine is credential-less and
store-only; FK edges are DB-declared and log-mined edges come from
query-log mining only.

## Wire contract (`GET /api/graph`, `DASHBOARD_SCHEMA_VERSION` 1.5 → 1.6)

```jsonc
{
  "source_connection_id": "<credential-safe label>",
  "nodes": [
    { "id": "order_item", "label": "order_item", "group": "activity",
      "catastrophic": false, "row_count": 481923 }
  ],
  "edges": [
    { "id": "order_item_to_order", "source": "order_item", "target": "order",
      "evidence": "declared", "canonical_path_rank": 1 }
  ],
  "canonical_path": { "nodes": ["order_item","order","user","tenant"],
                      "edges": ["order_item_to_order","order_to_user","user_to_tenant"],
                      "hops": 3 }
}
```

- GET-only (passes `assert_route_table_is_read_only`); charter `1.2` +
  `X-Schemabrain-Dashboard-Schema` headers stamped by middleware (not the
  handler).
- No resolvable source → **409** (reuses `_resolve_source`). A source that
  exists but has an empty projection → **200** with empty arrays.
- The resolved source is always passed through `_credential_safe_source_label`
  before it reaches the body — a URL-shaped override never leaks a password.

## Consequences

- **Positive:** the route is a clean pure-read of a persisted shape; the
  catastrophic floor can never disagree with the matrix; the canonical path
  is honest and deterministic; no new dependency; no schema migration
  (`SCHEMA_VERSION` stays `"15"` — the tables already exist).
- **Negative / accepted:** the persisted `is_catastrophic` snapshot is
  recomputed live by the route (justified defence-in-depth, above).
  Structural staleness after the deferred commands (`suggest --apply`, dbt
  import, init wizard) is a known gap closed by the next `index`/`apply` and
  by PR-22 for the wizard; it is documented, not silent.
- **Follow-up:** PR-17 consumes the persisted projection (server seed),
  emits rank-2 alternate paths, and renders the reactflow surface. The
  deferred rebuild hooks are tracked for a fast-follow.
