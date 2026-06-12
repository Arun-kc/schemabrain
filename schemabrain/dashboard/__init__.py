"""Read-only dashboard sidecar for SchemaBrain (v0.4 M1).

A FastAPI sidecar bound to ``127.0.0.1`` that serves three M1
surfaces (PII Visualization, Refusal Experience UI, Audit Viewer)
plus their inline trust badges. The sidecar reads from the same
SQLite store + ``mcp_audit`` table the CLI does and never writes.

Distribution: the sidecar deps (``fastapi`` / ``uvicorn`` /
``sse-starlette``) are gated behind ``pip install schemabrain[ui]``
so the base install stays free of web-server weight. The pre-built
Next.js static export ships inside the wheel at
``schemabrain/dashboard/static/`` and is served by the sidecar
under ``/``. End users never need Node.

This ``__init__`` is intentionally import-light — it must succeed on
a base install even when ``fastapi`` is not present. The sidecar
factory + CLI wiring live in submodules that import their heavy
deps lazily.
"""

from __future__ import annotations

from pathlib import Path

# Schema version of the dashboard's JSON contract (the read-only routes
# + SSE + health). Bumped when a route shape changes in a
# backward-incompatible way OR the shared type surface is extended
# (the TS↔Python parity matrix in `test_ts_charter_sync.py`). v0.4 M1
# shipped at 1.0; 1.1 adds the v15 `BindConfidence` union + the entity
# `rows` / `confidence` / `rationale` fields on /api/entities + drilldown;
# 1.2 adds the read-only `GET /api/pii/policy/preview` route + its
# `PolicyPreviewResponse` contract for the editable Policy surface;
# 1.3 adds the read-only `GET /api/drift` route + its `DriftResponse`
# contract for the Drift surface.
# 1.4 adds the per-column `columns` projection (column_name, entity,
# qualified_table, sensitivity, categories, advisory `pii_confidence`
# band) to `GET /api/entities/pii-matrix` for the confidence heatmap
# (ADR 0009).
# 1.5 adds the derived RFC-6962 Merkle layer over the audit chain: the
# read-only `GET /api/audit/merkle/root` (`MerkleRootResponse`) and
# `GET /api/audit/rows/{id}/proof` (`MerkleProofResponse`) routes, so a
# client can independently reconcile each audit row to one root.
# 1.6 adds the read-only `GET /api/graph` route + its `GraphResponse`
# contract (nodes / edges / canonical_path) — the backend half of the
# knowledge-graph surface, served from the v15 graph projection (ADR 0010).
# 1.7 enriches `GET /api/graph` for the graph surface (ADR 0011): each edge
# gains `cardinality` (declared FK edges only; null for mined/inferred) and
# each node replaces the `catastrophic` boolean with the full 5-state live
# `pii_level` (catastrophic | pii | confidential | internal | none).
# 1.8 adds the live refusal tally to `GET /api/graph` for the refusal-hotspot
# overlay (PR-17b): each node gains `refusal_count` and the response gains
# `unattributed_refusals`, aggregated per request from append-only `mcp_audit`
# `status='refused'` rows grouped by the non-canonical `anchor_entity` column
# (store v17). The attributed/unattributed split always reconciles with the
# audit log; the chained audit columns are never read or touched.
# 1.9 adds the read-only `GET /api/dict` route — the full data-dictionary
# model (schema version + every entity's binding, columns, semantic joins
# with the catastrophic-redacted ON clause, and metrics) serialised from the
# SAME `build_dictionary` aggregator the `schemabrain docs` CLI renders, so
# the Data Dictionary surface and its client-side Markdown export stay
# byte-for-byte aligned with the committed CLI golden.
# 1.10 adds the read-only `GET /api/overview` route — a single server-computed
# system-status summary behind the Overview ("home") surface. It rolls the
# honest, store-only signals the per-surface routes already expose (bound
# entities by group + confidence band, the per-column protection verdict
# reused from `/api/pii/policy`, drift freshness, 24h refusals, audit totals,
# semantic-layer counts) into one read so the home page renders in a single
# round trip. Every figure is derived live; absent values surface as null.
DASHBOARD_SCHEMA_VERSION = "1.10"

# Directory holding the static Next.js export. Populated at wheel-build
# time by the ``web/`` workspace's ``pnpm build`` output. At install
# time the wheel already contains the bytes; at contributor-dev time
# the directory holds a ``.gitkeep`` and a ``README.md`` placeholder
# (the dev server runs from ``web/`` directly on ``pnpm dev``).
STATIC_DIR = Path(__file__).parent / "static"

__all__ = ["DASHBOARD_SCHEMA_VERSION", "STATIC_DIR"]
