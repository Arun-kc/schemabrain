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
DASHBOARD_SCHEMA_VERSION = "1.4"

# Directory holding the static Next.js export. Populated at wheel-build
# time by the ``web/`` workspace's ``pnpm build`` output. At install
# time the wheel already contains the bytes; at contributor-dev time
# the directory holds a ``.gitkeep`` and a ``README.md`` placeholder
# (the dev server runs from ``web/`` directly on ``pnpm dev``).
STATIC_DIR = Path(__file__).parent / "static"

__all__ = ["DASHBOARD_SCHEMA_VERSION", "STATIC_DIR"]
