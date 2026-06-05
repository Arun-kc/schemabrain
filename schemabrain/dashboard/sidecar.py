"""FastAPI sidecar for the v0.4 dashboard.

The sidecar exposes a set of read-only JSON routes + 1 SSE stream + 1
health endpoint, plus serves the static Next.js export at ``/``.
Architectural invariants:

  - Bind ``127.0.0.1`` only. ``create_sidecar`` accepts no ``host``
    argument by design; the CLI launcher also does not surface one.
  - Read-only. No ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE`` handler
    is declared. A CI test in ``tests/dashboard/test_invariants.py``
    asserts this against the live route table.
  - Read-only deps. The sidecar imports its FastAPI / uvicorn / sse-
    starlette deps at call time, not at module-import time, so a base
    ``pip install schemabrain`` (without the ``[ui]`` extra) can still
    import this module's exports for type-checking and CLI dispatch
    without ImportError.

Route inventory (full spec in ``docs/internal/v0.4_ui_rfc.md`` §3):

  1. GET  /api/meta                            charter + sidecar info
  2. GET  /api/entities                        PII Viz matrix data
  3. GET  /api/entities/{name}/columns         PII Viz drill-down
  4. GET  /api/audit/rows                      Audit Viewer table
  5. GET  /api/audit/rows/{id}                 Audit Viewer drawer
  6. GET  /api/audit/verify                    chain verify status
  7. GET  /api/audit/refusals                  Refusal feed
  8. GET  /api/audit/refusals/{id}             Refusal envelope detail
  9. GET  /api/audit/merkle/root               derived Merkle root
 10. GET  /api/audit/rows/{id}/proof           inclusion proof
  SSE.   GET  /api/audit/stream                live audit row push
  H.     GET  /api/health                      liveness

(Additional read-only routes — entities/pii-matrix, pii/policy,
pii/policy/preview, drift — register in their own ``_register_*``
helpers below; the list above is the audit + entity core.)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from schemabrain.dashboard import DASHBOARD_SCHEMA_VERSION, STATIC_DIR

if TYPE_CHECKING:
    from fastapi import FastAPI


# Hard architectural invariant: the bind host is a constant, not a
# parameter. Even an operator passing ``--host`` would not change this;
# the CLI launcher does not expose a host flag.
BIND_HOST: str = "127.0.0.1"

# Default port chosen to avoid common dev-server collisions
# (3000 Next.js, 5173 Vite, 8000 Django/FastAPI default, 8080 generic).
# Operator can override via ``schemabrain dashboard --port``.
DEFAULT_PORT: int = 7878

# Custom response header stamped on every JSON response so a future
# client can detect protocol drift without parsing the body.
CHARTER_VERSION_HEADER: str = "X-Schemabrain-Charter-Version"

# Content-Security-Policy for the static dashboard, served on EVERY
# response (JSON, SSE, and the static export bytes).
#
#  - default-src 'self': nothing loads cross-origin by default.
#  - connect-src 'self': permits the same-origin SSE stream
#    (/api/audit/stream) and every /api fetch; blocks exfiltration to
#    a third-party origin.
#  - font-src 'self': the woff2 faces are self-hosted under _next/.
#  - img-src 'self' data:: inline SVG/data-URI assets the export emits.
#  - script-src / style-src include 'unsafe-inline' deliberately. A
#    Next.js `output: export` build emits inline bootstrap + RSC-flight
#    <script> tags plus the pre-hydration theme <script>, and there is
#    no server runtime to mint a per-request nonce (nonces need a
#    server; the sidecar serves verbatim bytes). A hash-based CSP IS
#    reachable but deferred: of the inline scripts only the RSC-flight
#    payload varies build-to-build, and only by the random Next build
#    id — pinning `generateBuildId` + a post-export hash-injection step
#    would let script-src drop 'unsafe-inline'. style-src is harder:
#    React inline style="" attributes fall under style-src-attr, which
#    hashes cannot cover, so they'd have to move to classes first. Until
#    that follow-up, 'unsafe-inline' is the pragmatic choice and the
#    residual XSS risk is bounded by the localhost-only bind, the
#    read-only route table, React's default escaping, and the absence of
#    any user-authored HTML. object-src 'none' + base-uri 'self' +
#    frame-ancestors 'none' keep the high-value sinks closed regardless.
CONTENT_SECURITY_POLICY: str = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "font-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline'",
        "connect-src 'self'",
        "form-action 'self'",
    ]
)

# Static, request-independent hardening headers applied to every
# response alongside the CSP. No HSTS: the sidecar serves plain HTTP on
# 127.0.0.1 with no TLS, so HSTS would be meaningless (and wrong) here.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

# SSE tick interval — matches the 2s polling story in RFC §6
# (deferred-items log). Real-time push is a v0.5 V5-2 item.
SSE_TICK_SECONDS: float = 2.0

# Drift sentinel file path — single source of truth shared with
# ``schemabrain/cli.py`` (the writer) and ``tests/test_cli_serve_sentinel.py``.
# CWD-relative on purpose: both ``schemabrain serve`` (writer) and the
# dashboard sidecar (reader) must resolve against the same working
# directory — the directory the operator invoked them from. Do NOT
# convert to an absolute path here; the path-match check in
# ``_compute_policy_drift`` already uses ``.resolve()`` for the
# equality test against the sentinel's recorded absolute path.
SERVE_POLICY_MTIME_SENTINEL_PATH: Path = Path("./schemabrain/.serve_policy_mtime")

# The canonical project-relative policy file (dbt-style project dir).
# Shared by the policy route and the drift route so the config-drift
# signal compares the SAME file `serve` recorded its mtime against.
DEFAULT_POLICY_YAML_PATH: Path = Path("./schemabrain/pii_policy.yaml")


def is_ui_available() -> bool:
    """``True`` if the ``[ui]`` extra is importable.

    Mirrors ``observability.otel.is_otel_available()``. Lets pytest skip
    sidecar tests when the extra is absent and lets the CLI surface a
    clean error instead of an ImportError stacktrace.
    """
    try:
        import fastapi  # noqa: F401
        import sse_starlette  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:  # pragma: no cover - the ImportError branch only
        # fires on a base install without [ui]; the CI matrix that runs
        # this test installs the extra so this path is the dual case
        # exercised by `feedback_full_ci_gate_before_push` users who don't
        # opt into the extra. Skipped tests cover the corresponding
        # observable behaviour.
        return False
    return True


@dataclass(frozen=True)
class SidecarConfig:
    """Read-only configuration passed to ``create_sidecar``.

    Frozen so a handler closing over the config cannot mutate it
    mid-request. Construction enforces invariants — the ``store_path``
    must exist (the sidecar is useless without a store to read from)
    and the ``port`` must be in the user-port range (1024-65535).
    """

    store_path: Path
    port: int = DEFAULT_PORT
    source_connection_id: str | None = None

    def __post_init__(self) -> None:
        if not self.store_path.exists():
            raise ValueError(
                f"store_path does not exist: {self.store_path}. "
                f"Run `schemabrain index` against your source DB first."
            )
        if not (1024 <= self.port <= 65535):
            raise ValueError(f"port must be in the user-port range 1024-65535 (got {self.port})")


def create_sidecar(config: SidecarConfig) -> FastAPI:
    """Build the FastAPI app for the dashboard sidecar.

    Imports FastAPI lazily so a base ``pip install schemabrain``
    (without ``[ui]``) can still import this module's exports.
    Calling ``create_sidecar`` on a base install raises ``ImportError``
    with an actionable message naming the extra.
    """
    import mimetypes

    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("application/javascript", ".js")

    try:
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - tested by E2E
        raise ImportError(
            "schemabrain dashboard requires the [ui] extra. Install with "
            "`pip install schemabrain[ui]`."
        ) from exc

    app = FastAPI(
        title="SchemaBrain Dashboard",
        version=DASHBOARD_SCHEMA_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Stamp the boot time on the app so /api/health can compute uptime
    # without an extra import or a module-level mutable global.
    app.state.boot_time = time.monotonic()
    app.state.config = config

    _register_invariant_headers(app)
    _register_health_route(app)
    _register_meta_route(app, config)
    _register_entity_routes(app, config)
    _register_audit_routes(app, config)
    _register_policy_route(app, config)
    _register_drift_route(app, config)
    _register_graph_route(app, config)
    _register_stream_route(app, config)

    has_static_export = STATIC_DIR.exists() and any(
        p.name not in {".gitkeep", "README.md"} for p in STATIC_DIR.iterdir()
    )
    if has_static_export:
        # End-user install path: the wheel ships the Next.js export,
        # the mount registers, the full React UI loads.
        #
        # Next.js with `output: "export"` emits `pii.html` / `audit.html`
        # at the file level, but routes the React app generates link to
        # `/pii` / `/audit` (no `.html` suffix). FastAPI's `StaticFiles`
        # `html=True` flag handles directory-index lookups (`/` →
        # `/index.html`) but does NOT rewrite `/pii` → `/pii.html` on
        # 404. The middleware below adds that one missing piece — it
        # lets a single static export work without changing Next.js's
        # default URL aesthetic or forcing trailing slashes everywhere.
        _register_html_fallback(app)
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    else:
        # Dev / pre-build path: serve a minimal landing page so an
        # operator who boots the sidecar without a static export gets
        # a working surface to poke at instead of a bare 404. The page
        # shows live data via API fetches and explains how to get the
        # full React UI.
        _register_fallback_landing(app)

    # Registered LAST so it is the OUTERMOST middleware (Starlette wraps
    # most-recently-added first), which means it stamps EVERY response —
    # including the html-fallback FileResponse, which short-circuits and
    # returns before the inner middleware stack ever runs. A static
    # surface (`/pii`) must carry the CSP just as the JSON routes do.
    _register_security_headers(app)

    return app


def _register_security_headers(app: FastAPI) -> None:
    """Attach the CSP + hardening headers to every response.

    Registered first so it is the outermost middleware (LIFO on the
    response path), which means it stamps EVERY response — the JSON
    routes, the SSE stream, the static-export bytes, and the HTML
    fallback's ``FileResponse`` alike.
    """
    from starlette.requests import Request
    from starlette.responses import Response

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


def _register_invariant_headers(app: FastAPI) -> None:
    """Attach the charter-version header to every response."""
    from starlette.requests import Request
    from starlette.responses import Response

    @app.middleware("http")
    async def add_charter_header(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers[CHARTER_VERSION_HEADER] = "1.2"
        response.headers["X-Schemabrain-Dashboard-Schema"] = DASHBOARD_SCHEMA_VERSION
        return response


def _register_health_route(app: FastAPI) -> None:
    """GET /api/health — liveness + store probe."""

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        store_path = app.state.config.store_path
        store_state = "ok"
        store_reason: str | None = None
        try:
            conn = sqlite3.connect(str(store_path))
            conn.execute("SELECT 1").fetchone()
            conn.close()
        except sqlite3.Error as exc:
            store_state = "degraded"
            store_reason = str(exc)
        uptime_s = round(time.monotonic() - app.state.boot_time, 3)
        return {
            "status": "ok",
            "store": store_state,
            "store_reason": store_reason,
            "uptime_s": uptime_s,
        }


def _register_meta_route(app: FastAPI, config: SidecarConfig) -> None:
    """GET /api/meta — charter + sidecar info."""
    from schemabrain.audit import FINGERPRINT_VERSION
    from schemabrain.core.store import SQLiteStore
    from schemabrain.mcp.envelope import CHARTER_VERSION

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        from schemabrain.core.source_id import (
            looks_like_connection_url,
            make_source_id,
        )

        with SQLiteStore(config.store_path) as store:
            source_ids = store.list_distinct_source_connection_ids()
            summaries = store.summarize_sources()
        # Coerce config.source_connection_id to a credential-safe form
        # before echoing it back. If it's a connection URL, hash it to
        # the canonical short ID; otherwise treat it as a pre-computed
        # label and pass through. Never emit the raw URL — it carries
        # the DB password.
        #
        # The dialect is only knowable for THIS configured source: the
        # store persists hashed ids, never the URL, so other sources
        # report engine=None. `looks_like_connection_url` matches only
        # Postgres schemes, so a recognized URL implies engine=postgres.
        configured = config.source_connection_id
        configured_id: str | None = None
        configured_engine: str | None = None
        if configured and looks_like_connection_url(configured):
            try:
                configured_id = make_source_id(configured)
                configured_engine = "postgres"
            except ValueError:  # pragma: no cover - defensive: the
                # looks_like_connection_url guard already validated a
                # Postgres scheme, so make_source_id cannot raise here.
                # Kept belt-and-suspenders: refuse to echo a partial URL
                # rather than leak credentials; fall back to the store list.
                configured_id = None
        elif configured:
            # A pre-computed source_id / label, already credential-free.
            configured_id = configured

        default_source = configured_id or (source_ids[0] if source_ids else None)

        # Per-source rollup for the shell source selector. `state` is
        # always "indexed": a source only appears once it has persisted
        # artifacts, and there is no in-flight indexing registry to back
        # an "indexing"/"empty" value — the field stays a typed enum for
        # forward use but is never fabricated. `last_indexed_at` is None
        # for a source with no physical tables (UI renders '—').
        sources = [
            {
                "source_id": s.source_connection_id,
                "engine": (configured_engine if s.source_connection_id == configured_id else None),
                "state": "indexed",
                "last_indexed_at": s.last_indexed_at,
                "tables": s.table_count,
                "entities": s.entity_count,
            }
            for s in summaries
        ]

        return {
            "charter_version": CHARTER_VERSION,
            "dashboard_schema_version": DASHBOARD_SCHEMA_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
            "store_path": str(config.store_path),
            "default_source_connection_id": default_source,
            "source_connection_ids": source_ids,
            "sources": sources,
        }


# Per-entity PII severity ladder for the entities-list rollup + drilldown
# header. "catastrophic" outranks the 4-level sensitivity ladder so the
# entities index can render one decisive badge per entity.
_PII_LEVEL_SENSITIVITY_ORDER: tuple[str, ...] = ("pii", "confidential", "internal")

# How many nearest-neighbour columns to surface per drilldown column.
# Bounded small: the drilldown "similar" hint is a few candidates, not a
# ranked catalogue, and each lookup is a per-source cosine scan.
_SIMILAR_COLUMNS_K: int = 5


def _pii_level_from_tags(
    tag_values: Iterable[tuple[str, frozenset[str]]],
    *,
    catastrophic_categories: frozenset[str],
) -> str:
    """Collapse a table's PII tags into one entity-level severity label.

    Returns "catastrophic" when any column carries a catastrophic-leak
    category; otherwise the highest sensitivity present on the 4-level
    ladder ("pii" > "confidential" > "internal"); "none" when every column
    is public/untagged. Mirrors the matrix route's catastrophic +
    sensitivity bucketing so the two surfaces never disagree on severity.
    """
    sensitivities: set[str] = set()
    for sensitivity, categories in tag_values:
        if categories & catastrophic_categories:
            return "catastrophic"
        sensitivities.add(sensitivity)
    for level in _PII_LEVEL_SENSITIVITY_ORDER:
        if level in sensitivities:
            return level
    return "none"


def _entity_pii_level(
    store: Any,
    entity: Any,
    *,
    source_connection_id: str,
    catastrophic_categories: frozenset[str],
) -> str:
    """Resolve one entity's PII severity label from its bound table's tags."""
    schema_name, table_name = entity.qualified_table.split(".", 1)
    table = store.get_table(schema_name, table_name, source_connection_id=source_connection_id)
    if table is None:  # pragma: no cover - defensive; FK guarantees the table
        return "none"
    column_names = [c.name for c in table.columns]
    tags = store.get_column_pii_tags(
        source_connection_id=source_connection_id,
        qualified_table=entity.qualified_table,
        columns=column_names,
    )
    return _pii_level_from_tags(tags.values(), catastrophic_categories=catastrophic_categories)


def _reconstruct_canonical_path(edges: list[Any]) -> dict[str, Any]:
    """Order the rank-1 (primary canonical) edges into a hop sequence.

    A trivial walk over the persisted spine edges — NOT a re-walk of the
    full graph (ADR 0010). Only `canonical_path_rank == 1` (the primary
    path) is used; rank-2 alternates (reserved for PR-17) are deliberately
    excluded so they never fold the primary path into a cycle. The spine
    is a simple path by construction (`longest_canonical_path`), so it has
    exactly two degree-1 endpoints; the walk starts from the
    lexicographically smaller one for determinism. Returns the empty path
    when there is no spine.
    """
    spine = [edge for edge in edges if edge.canonical_path_rank == 1]
    if not spine:
        return {"nodes": [], "edges": [], "hops": 0}
    adjacency: dict[str, list[tuple[str, str]]] = {}
    degree: dict[str, int] = {}
    for edge in spine:
        adjacency.setdefault(edge.source_entity, []).append((edge.target_entity, edge.join_name))
        adjacency.setdefault(edge.target_entity, []).append((edge.source_entity, edge.join_name))
        degree[edge.source_entity] = degree.get(edge.source_entity, 0) + 1
        degree[edge.target_entity] = degree.get(edge.target_entity, 0) + 1
    endpoints = sorted(name for name, count in degree.items() if count == 1)
    if len(endpoints) != 2:  # pragma: no cover — the spine is always a simple path
        return {"nodes": [], "edges": [], "hops": 0}
    nodes = [endpoints[0]]
    edge_names: list[str] = []
    visited = {endpoints[0]}
    current = endpoints[0]
    while True:
        unvisited = [(nb, jn) for nb, jn in adjacency[current] if nb not in visited]
        if not unvisited:
            break
        neighbor, join_name = unvisited[0]
        nodes.append(neighbor)
        edge_names.append(join_name)
        visited.add(neighbor)
        current = neighbor
    return {"nodes": nodes, "edges": edge_names, "hops": len(edge_names)}


def _register_entity_routes(app: FastAPI, config: SidecarConfig) -> None:
    """PII Viz routes — list entities + per-entity column drill-down."""
    from fastapi import HTTPException

    from schemabrain.core.store import SQLiteStore
    from schemabrain.mcp._redaction import render_join_on_clause
    from schemabrain.pii.categories import (
        CATASTROPHIC_LEAK_CATEGORIES,
        PII_CATEGORIES_ORDERED,
    )

    @app.get("/api/entities/pii-matrix")
    def pii_matrix_route(source_connection_id: str | None = None) -> dict[str, Any]:
        """Aggregated entity x PII-category counts for The Ledger surface.

        One round-trip replaces the (N entities) x (1 columns fetch) =
        N+1 fan-out the matrix view would otherwise need. Per RFC §5.1,
        each row reports per-category column counts + a derived
        `has_catastrophic` flag the surface uses to render the gutter
        tick. Bulk totals at the bottom drive the footer summary +
        the "N columns carry catastrophic-leak categories" headline.

        The shape is intentionally close to the wire shape the React
        Server Component renders. Future bulk pagination
        (catastrophic-only filter, sticky header) layers on top.
        """
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            entities = store.list_entities(source_connection_id=resolved_source)
            matrix_entries: list[dict[str, Any]] = []
            column_entries: list[dict[str, Any]] = []
            total_columns = 0
            total_pii = 0
            total_confidential = 0
            total_internal_or_public = 0
            total_catastrophic_columns = 0
            for entity in entities:
                schema_name, table_name = entity.qualified_table.split(".", 1)
                table = store.get_table(
                    schema_name,
                    table_name,
                    source_connection_id=resolved_source,
                )
                column_names = [c.name for c in table.columns] if table else []
                tags = store.get_column_pii_tags(
                    source_connection_id=resolved_source,
                    qualified_table=entity.qualified_table,
                    columns=column_names,
                )
                # ADR 0009: per-column advisory confidence band, read
                # from the index-time-populated column. Absent rows
                # (public columns / pre-spike stores) read back as
                # `(None, None)` — the band renders "—", never a faked 0.
                confidence = store.get_column_pii_confidence(
                    source_connection_id=resolved_source,
                    qualified_table=entity.qualified_table,
                    columns=column_names,
                )
                # Counts: category → number of columns in this entity
                # that carry that category. Sensitivity counters
                # accumulate at the entity level (1 column = 1
                # increment of its sensitivity bucket). Iterating the
                # ordered tuple (not the frozenset) keeps JSON key
                # order deterministic across processes — audit log
                # snapshots stay stable, and any consumer that
                # happens to walk Object.entries() gets the same
                # canonical column order as the matrix headers.
                counts: dict[str, int] = dict.fromkeys(PII_CATEGORIES_ORDERED, 0)
                catastrophic_columns_here = 0
                for col_name in column_names:
                    tag = tags.get(col_name)
                    total_columns += 1
                    # Only the band is surfaced — the raw score stays
                    # internal (no numeric % in the UI; the band is the
                    # display contract, see ADR 0009 / §7 #6).
                    band = confidence.get(col_name, (None, None))[0]
                    if tag is None:
                        total_internal_or_public += 1
                        column_entries.append(
                            {
                                "entity": entity.name,
                                "qualified_table": entity.qualified_table,
                                "column_name": col_name,
                                "sensitivity": "public",
                                "categories": [],
                                "pii_confidence": band,
                            }
                        )
                        continue
                    sensitivity, categories = tag
                    if sensitivity == "pii":
                        total_pii += 1
                    elif sensitivity == "confidential":
                        total_confidential += 1
                    else:
                        total_internal_or_public += 1
                    for cat in categories:
                        if cat in counts:
                            counts[cat] += 1
                    if any(c in CATASTROPHIC_LEAK_CATEGORIES for c in categories):
                        catastrophic_columns_here += 1
                    column_entries.append(
                        {
                            "entity": entity.name,
                            "qualified_table": entity.qualified_table,
                            "column_name": col_name,
                            "sensitivity": sensitivity,
                            "categories": [c for c in PII_CATEGORIES_ORDERED if c in categories],
                            "pii_confidence": band,
                        }
                    )
                has_catastrophic = catastrophic_columns_here > 0
                total_catastrophic_columns += catastrophic_columns_here
                matrix_entries.append(
                    {
                        "name": entity.name,
                        "qualified_table": entity.qualified_table,
                        "identity": entity.identity,
                        "origin": entity.origin,
                        "inference_method": entity.inference_method,
                        "validation_state": entity.validation_state,
                        "counts": counts,
                        "catastrophic_column_count": catastrophic_columns_here,
                        "has_catastrophic": has_catastrophic,
                    }
                )
        return {
            "source_connection_id": resolved_source,
            "entities": matrix_entries,
            # Per-column projection (ADR 0009) for the column x category
            # confidence heatmap — one entry per entity-bound column,
            # canonical category order, advisory band (or null).
            "columns": column_entries,
            "categories": list(PII_CATEGORIES_ORDERED),
            "catastrophic_categories": sorted(CATASTROPHIC_LEAK_CATEGORIES),
            "totals": {
                "entities": len(matrix_entries),
                "columns": total_columns,
                "catastrophic_columns": total_catastrophic_columns,
                "pii_columns": total_pii,
                "confidential_columns": total_confidential,
                "internal_or_public_columns": total_internal_or_public,
            },
        }

    @app.get("/api/entities")
    def list_entities_route(
        source_connection_id: str | None = None,
    ) -> dict[str, Any]:
        """Entities index: per-entity rollup + a summary strip.

        Each item carries the entity's `group`, a single `pii_level`
        badge, and `metrics_count` / `joins_count` so the index renders
        without an N+1 drill per row — metrics + joins are fetched once
        and counted in Python. `rows` (the v15 `estimated_row_count` for
        the bound table) and `confidence` (the v15 `bind_confidence`
        model self-rating) ride the same single-fetch path: row counts
        are read once for the whole source and `confidence` comes off
        the already-loaded entity. Both are `null` when unpopulated
        (hand-authored entity / SQLite source / never-analyzed table) so
        the UI renders "—" rather than a fabricated count.
        """
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            entities = store.list_entities(source_connection_id=resolved_source)
            all_metrics = store.list_metrics(source_connection_id=resolved_source)
            all_joins = store.list_canonical_joins(source_connection_id=resolved_source)
            row_counts = store.estimated_row_counts(source_connection_id=resolved_source)
            items: list[dict[str, Any]] = []
            catastrophic_entities = 0
            for e in entities:
                pii_level = _entity_pii_level(
                    store,
                    e,
                    source_connection_id=resolved_source,
                    catastrophic_categories=CATASTROPHIC_LEAK_CATEGORIES,
                )
                if pii_level == "catastrophic":
                    catastrophic_entities += 1
                items.append(
                    {
                        "name": e.name,
                        "description": e.description,
                        "qualified_table": e.qualified_table,
                        "identity": e.identity,
                        "group": e.group,
                        "origin": e.origin,
                        "inference_method": e.inference_method,
                        "validation_state": e.validation_state,
                        "rows": row_counts.get(e.qualified_table),
                        "confidence": e.bind_confidence,
                        "pii_level": pii_level,
                        "metrics_count": sum(1 for m in all_metrics if m.entity == e.name),
                        "joins_count": sum(
                            1 for j in all_joins if e.name in (j.source_entity, j.target_entity)
                        ),
                    }
                )
        return {
            # Coerce a URL-shaped override to its canonical hashed id before
            # echoing — `_resolve_source` returns an operator override verbatim,
            # so a connection URL (carrying the DB password) must not reach the
            # body. Mirrors /api/meta and /api/drift; the store queries above
            # still use the real `resolved_source`.
            "source_connection_id": _credential_safe_source_label(resolved_source),
            "items": items,
            "count": len(items),
            "summary": {
                "entities": len(items),
                "metrics": len(all_metrics),
                "joins": len(all_joins),
                "catastrophic_entities": catastrophic_entities,
            },
        }

    @app.get("/api/entities/{name}/columns")
    def entity_columns_route(name: str, source_connection_id: str | None = None) -> dict[str, Any]:
        """Entity drilldown: physical columns + semantic metrics/joins.

        Columns carry `data_type`, the free-text `description` (the human
        "meaning", null when un-enriched), and `similar` (column→column
        nearest neighbours elsewhere in the schema — structured, no
        free-text reason that could leak a value). Joins carry a
        server-rendered, catastrophic-redacted `on_clause` (the client no
        longer assembles it), plus `cardinality`, `origin`,
        `inference_method`, and a derived `is_log_mined` flag. A
        `referenced_by` rollup counts the anchored metrics + touching joins.
        """
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            entity = store.get_entity(name, source_connection_id=resolved_source)
            if entity is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"entity {name!r} not found for source {resolved_source!r}",
                )
            # Both join endpoints must resolve to render the redacted ON
            # clause; one list_entities call serves every join on the page.
            entity_by_name = {
                e.name: e for e in store.list_entities(source_connection_id=resolved_source)
            }
            schema_name, table_name = entity.qualified_table.split(".", 1)
            table = store.get_table(schema_name, table_name, source_connection_id=resolved_source)
            columns: list[dict[str, Any]] = []
            if table is None:  # pragma: no cover - defensive
                # An entity bound to a table that's no longer indexed —
                # ship the entity with zero columns rather than 500.
                # Unreachable under the store's FK guarantee
                # (`delete_table` cascades to entities), but kept as a
                # belt-and-suspenders guard against a future store
                # backend that breaks the cascade.
                pass
            else:
                ordered = sorted(table.columns, key=lambda c: c.ordinal_position)
                raw_tags = store.get_column_pii_tags(
                    source_connection_id=resolved_source,
                    qualified_table=entity.qualified_table,
                    columns=[c.name for c in ordered],
                )
                descriptions = store.get_table_descriptions(
                    schema_name, table_name, source_connection_id=resolved_source
                )
                for col in ordered:
                    sensitivity, categories = raw_tags.get(col.name, ("public", frozenset()))
                    description = descriptions.get(col.name)
                    similar = store.nearest_columns(
                        schema_name,
                        table_name,
                        col.name,
                        source_connection_id=resolved_source,
                        k=_SIMILAR_COLUMNS_K,
                    )
                    columns.append(
                        {
                            "name": col.name,
                            "data_type": col.data_type,
                            "description": description.text if description is not None else None,
                            "sensitivity": sensitivity,
                            "pii_categories": sorted(categories),
                            "similar": [
                                {"schema": s, "table": t, "column": c, "score": score}
                                for s, t, c, score in similar
                            ],
                        }
                    )

            all_metrics = store.list_metrics(source_connection_id=resolved_source)
            all_joins = store.list_canonical_joins(source_connection_id=resolved_source)

            metrics = [
                {
                    "name": m.name,
                    "description": m.description,
                    "measure": {
                        "agg": m.measure.agg,
                        "column": m.measure.column,
                        "expression": m.measure.expression,
                    },
                    "time_grains": m.time_grains,
                }
                for m in all_metrics
                if m.entity == name
            ]

            # ON clauses render inside the with-block — the shared helper
            # queries PII tags to mask catastrophic FK column names.
            joins = [
                {
                    "name": j.name,
                    "description": j.description,
                    "source_entity": j.source_entity,
                    "target_entity": j.target_entity,
                    "on_clause": render_join_on_clause(
                        j,
                        store=store,
                        source_connection_id=resolved_source,
                        entity_by_name=entity_by_name,
                    ),
                    "cardinality": j.cardinality,
                    "origin": j.origin,
                    "inference_method": j.inference_method,
                    "is_log_mined": j.inference_method == "observed_in_query_log",
                }
                for j in all_joins
                if j.source_entity == name or j.target_entity == name
            ]

            # Read the bound table's row estimate inside the with-block —
            # the store closes before the return is built.
            entity_rows = store.estimated_row_counts(source_connection_id=resolved_source).get(
                entity.qualified_table
            )

        return {
            "entity": {
                "name": entity.name,
                "description": entity.description,
                "qualified_table": entity.qualified_table,
                "identity": entity.identity,
                "group": entity.group,
                "origin": entity.origin,
                "inference_method": entity.inference_method,
                "validation_state": entity.validation_state,
                # v15 model self-rating + row estimate. `rows` is the
                # bound table's cached `estimated_row_count` (null when
                # unknown); `confidence` is the model's `bind_confidence`
                # (null for hand-authored). `rationale` maps the empty
                # default to null so the UI omits it rather than rendering
                # a blank line.
                "rows": entity_rows,
                "confidence": entity.bind_confidence,
                "rationale": entity.rationale or None,
            },
            "columns": columns,
            "metrics": metrics,
            "joins": joins,
            "referenced_by": {
                "metrics": len(metrics),
                "joins": len(joins),
            },
        }


def _register_audit_routes(app: FastAPI, config: SidecarConfig) -> None:
    """Audit routes — rows, refusals, verify."""
    from fastapi import HTTPException

    from schemabrain.audit.merkle import inclusion_proof, leaf_hash, merkle_root
    from schemabrain.audit.verify import (
        SinceCursorError,
        resolve_since_cursor,
        walk_chain,
    )

    @app.get("/api/audit/rows")
    def audit_rows_route(
        limit: int = 50,
        offset: int = 0,
        since: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        limit, offset = _validate_pagination(limit, offset)
        with _open_audit_conn(config) as conn:
            cursor_id = _maybe_resolve_since(conn, since)
            rows = _select_audit_rows(
                conn,
                limit=limit,
                offset=offset,
                after_id=cursor_id,
                status_filter=status,
            )
            total = _count_audit_rows(conn, after_id=cursor_id, status_filter=status)
        return {
            "items": [_serialize_audit_row(r) for r in rows],
            "limit": limit,
            "offset": offset,
            "total": total,
            "since_cursor_id": cursor_id,
        }

    @app.get("/api/audit/rows/{row_id}")
    def audit_row_detail_route(row_id: int) -> dict[str, Any]:
        with _open_audit_conn(config) as conn:
            row = conn.execute("SELECT * FROM mcp_audit WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                raise HTTPException(404, detail=f"audit row {row_id} not found")
            prev_row = conn.execute(
                "SELECT chain_hash FROM mcp_audit WHERE id = ? - 1", (row_id,)
            ).fetchone()
        body = _serialize_audit_row(row)
        body["prev_chain_hash_hex"] = bytes(prev_row["chain_hash"]).hex() if prev_row else None
        return body

    @app.get("/api/audit/verify")
    def audit_verify_route(since: str | None = None, full: bool = False) -> dict[str, Any]:
        with _open_audit_conn(config) as conn:
            cursor_id: int | None = None
            if since is not None:
                try:
                    cursor_id = resolve_since_cursor(conn, since)
                except (SinceCursorError, ValueError) as exc:
                    raise HTTPException(400, detail=str(exc)) from exc
            mismatches = list(walk_chain(conn, full=full, start_after_row_id=cursor_id))
            tail = conn.execute("SELECT max(id) AS m FROM mcp_audit").fetchone()
        status = "intact" if not mismatches else "broken"
        return {
            "status": status,
            "walked_through_id": tail["m"] if tail else None,
            "cursor_id": cursor_id,
            "mismatches": [
                {
                    "row_id": m.row_id,
                    "expected_hex": m.expected_hex,
                    "actual_hex": m.actual_hex,
                }
                for m in mismatches
            ],
        }

    @app.get("/api/audit/refusals")
    def audit_refusals_route(
        limit: int = 50, offset: int = 0, since: str | None = None
    ) -> dict[str, Any]:
        limit, offset = _validate_pagination(limit, offset)
        with _open_audit_conn(config) as conn:
            cursor_id = _maybe_resolve_since(conn, since)
            rows = _select_audit_rows(
                conn,
                limit=limit,
                offset=offset,
                after_id=cursor_id,
                status_filter="refused",
            )
            total = _count_audit_rows(conn, after_id=cursor_id, status_filter="refused")
        return {
            "items": [_serialize_refusal(r) for r in rows],
            "limit": limit,
            "offset": offset,
            "total": total,
        }

    @app.get("/api/audit/refusals/{row_id}")
    def refusal_detail_route(row_id: int) -> dict[str, Any]:
        with _open_audit_conn(config) as conn:
            row = conn.execute(
                "SELECT * FROM mcp_audit WHERE id = ? AND status = 'refused'",
                (row_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    404,
                    detail=f"refusal row {row_id} not found (or not a refused row)",
                )
        return _serialize_refusal(row, include_envelope=True)

    @app.get("/api/audit/merkle/root")
    def audit_merkle_root_route() -> dict[str, Any]:
        """Derived RFC-6962 Merkle root over every audit row (id-ASC).

        Stateless: recomputed per request from the same canonical leaf
        preimages the chain consumes. An empty log returns the SHA-256 of
        the empty string (never null, never all-zeros).
        """
        with _open_audit_conn(config) as conn:
            leaves = _audit_leaf_preimages(conn)
        return {"tree_size": len(leaves), "root_hex": merkle_root(leaves).hex()}

    @app.get("/api/audit/rows/{row_id}/proof")
    def audit_row_proof_route(row_id: int) -> dict[str, Any]:
        """Inclusion proof for one audit row against the current root.

        ``leaf_index`` is the 0-based position in id-ASC order — computed
        server-side from the row's id rank, NOT from the id-DESC render
        order of ``/api/audit/rows``. 404 if the id is absent.
        """
        with _open_audit_conn(config) as conn:
            exists = conn.execute("SELECT 1 FROM mcp_audit WHERE id = ?", (row_id,)).fetchone()
            if exists is None:
                raise HTTPException(404, detail=f"audit row {row_id} not found")
            rank = conn.execute(
                "SELECT COUNT(*) AS c FROM mcp_audit WHERE id < ?", (row_id,)
            ).fetchone()
            leaf_index = int(rank["c"])
            leaves = _audit_leaf_preimages(conn)
        return {
            "leaf_index": leaf_index,
            "tree_size": len(leaves),
            "root_hex": merkle_root(leaves).hex(),
            "leaf_hash_hex": leaf_hash(leaves[leaf_index]).hex(),
            "audit_path": [h.hex() for h in inclusion_proof(leaf_index, leaves)],
        }


def _audit_leaf_preimages(conn: sqlite3.Connection) -> list[bytes]:
    """Return every ``mcp_audit`` row's canonical leaf preimage, id-ASC.

    Reuses ``verify._ROW_COLUMNS`` + ``verify._row_to_canonical_dict`` +
    ``canonical_audit_row`` — the EXACT serialisation path the chain walk
    uses (``verify.py:126-127``) — so the Merkle tree and the hash chain
    can never disagree on a row's bytes. id-ASC matches the chain order;
    the resulting list index is the leaf index.
    """
    from fastapi import HTTPException

    from schemabrain.audit.canonical import canonical_audit_row
    from schemabrain.audit.verify import _ROW_COLUMNS, _row_to_canonical_dict

    # `_ROW_COLUMNS` is a hardcoded module-level tuple of column names; no
    # operator input enters the SELECT. ORDER BY id ASC matches walk_chain.
    select_cols = ", ".join(_ROW_COLUMNS)
    rows = conn.execute(
        f"SELECT {select_cols} FROM mcp_audit ORDER BY id ASC"  # nosec B608 — column list hardcoded
    )
    leaves: list[bytes] = []
    for row in rows:
        try:
            leaves.append(canonical_audit_row(_row_to_canonical_dict(row)))
        except ValueError as exc:
            # A row whose canonical shape is broken (corruption / future-schema
            # drift) cannot be a deterministic leaf. Surface it as an actionable
            # 422 rather than a 500 — `audit verify --full` reports exactly which
            # row, mirroring verify.py's graceful posture toward such rows.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"audit row {row['id']} failed canonicalisation ({exc}); "
                    "run `schemabrain audit verify --full` to inspect the chain"
                ),
            ) from exc
    return leaves


def _compute_policy_drift(yaml_path: Path) -> dict[str, Any]:
    """Compare ``schemabrain serve``'s recorded YAML mtime against
    the current on-disk YAML mtime.

    Shared by the policy route (``/api/pii/policy``) and the drift route
    (``/api/drift``) — both surface the same config-drift signal, so the
    computation lives at module level rather than inside either closure.

    Returns a dict with stable keys:

        {
            "detected": bool,
            "recorded_at": iso8601 | None,
            "current_mtime": iso8601 | None,
        }

    ``detected`` fires when:
      - sentinel records ``yaml_existed_at_boot=True`` AND the
        current YAML is missing (operator removed the file),
      - sentinel mtime differs from the current YAML mtime by
        more than 1ms (operator edited the file),
      - sentinel records ``yaml_existed_at_boot=False`` AND a
        YAML appears now (operator created the file).

    Returns ``detected=False`` when:
      - no sentinel exists (no serve running OR serve used
        ``--pii-block`` CLI override),
      - sentinel's recorded ``policy_path`` does not resolve to
        ``yaml_path`` (serve was watching a different file),
      - any stat/parse error (fail-closed: better silent than
        misleading).
    """
    # `json` is imported at module level; `datetime` is deferred here
    # (only this drift computation needs it).
    from datetime import UTC, datetime

    def _iso_from_mtime(mtime: float) -> str:
        return datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")

    def _current_mtime_iso() -> str | None:
        try:
            return _iso_from_mtime(yaml_path.stat().st_mtime)
        except OSError:
            return None

    # Collapse the exists-check + read into one open to eliminate
    # the TOCTOU window between them. FileNotFoundError is the
    # "no sentinel" steady state; any other OSError + malformed
    # JSON is fail-closed.
    try:
        sentinel_text = SERVE_POLICY_MTIME_SENTINEL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "detected": False,
            "recorded_at": None,
            "current_mtime": _current_mtime_iso(),
        }
    except OSError:
        return {
            "detected": False,
            "recorded_at": None,
            "current_mtime": _current_mtime_iso(),
        }

    try:
        payload = json.loads(sentinel_text)
    except json.JSONDecodeError:
        return {
            "detected": False,
            "recorded_at": None,
            "current_mtime": _current_mtime_iso(),
        }

    # Type-narrow every field read from the JSON payload. The
    # sentinel writer is well-behaved, but a corrupt or hand-edited
    # file can carry any JSON type for any key — and an unguarded
    # float() on a string crashes the whole route.
    raw_recorded_iso = payload.get("recorded_at_iso") if isinstance(payload, dict) else None
    recorded_iso = raw_recorded_iso if isinstance(raw_recorded_iso, str) else None
    recorded_mtime = payload.get("recorded_at_mtime") if isinstance(payload, dict) else None
    sentinel_path = payload.get("policy_path") if isinstance(payload, dict) else None
    yaml_existed = bool(payload.get("yaml_existed_at_boot") if isinstance(payload, dict) else False)

    try:
        resolved_yaml = str(yaml_path.expanduser().resolve())
    except OSError:
        return {
            "detected": False,
            "recorded_at": recorded_iso,
            "current_mtime": None,
        }

    # Sentinel watched a different file — drift signal isn't meaningful.
    if sentinel_path != resolved_yaml:
        return {
            "detected": False,
            "recorded_at": recorded_iso,
            "current_mtime": _current_mtime_iso(),
        }

    # Collapse exists/stat into a single stat that surfaces
    # FileNotFoundError separately from other OSError so the
    # deletion-drift branch preserves the yaml_existed context
    # even if the file disappears in a TOCTOU race.
    try:
        current_mtime_float = yaml_path.stat().st_mtime
    except FileNotFoundError:
        return {
            "detected": yaml_existed,
            "recorded_at": recorded_iso,
            "current_mtime": None,
        }
    except OSError:  # pragma: no cover — defensive; same fail-closed posture as the read_text / resolve OSError branches above which ARE pinned
        return {
            "detected": False,
            "recorded_at": recorded_iso,
            "current_mtime": None,
        }

    current_iso = _iso_from_mtime(current_mtime_float)
    if recorded_mtime is None:
        # Serve booted with no YAML; a YAML appeared since — but
        # only honour that interpretation when the sentinel agrees
        # yaml DID NOT exist at boot. A sentinel claiming
        # yaml_existed_at_boot=True with recorded_mtime=None is
        # internally inconsistent (the writer cannot produce it);
        # treat that as fail-closed rather than false-positive drift.
        return {
            "detected": not yaml_existed,
            "recorded_at": recorded_iso,
            "current_mtime": current_iso,
        }

    # 1ms epsilon to avoid float precision false positives. The
    # float() coercion can raise ValueError (string like "abc") or
    # TypeError (list/dict) on a corrupt sentinel — fail-closed so
    # the route stays available even if the sentinel is mangled.
    try:
        recorded_mtime_float = float(recorded_mtime)
    except (TypeError, ValueError):
        return {
            "detected": False,
            "recorded_at": recorded_iso,
            "current_mtime": current_iso,
        }
    drifted = abs(current_mtime_float - recorded_mtime_float) > 0.001
    return {
        "detected": drifted,
        "recorded_at": recorded_iso,
        "current_mtime": current_iso,
    }


def _register_policy_route(app: FastAPI, config: SidecarConfig) -> None:
    """GET /api/pii/policy — operator-editable PII enforcement state.

    Returns the active block set + per-column tag listing with
    provenance. Mirrors the data the CLI `policy show` renders so
    the dashboard view and the CLI stay symmetric.

    `block_source` is `"yaml"` when a pii_policy.yaml file was found
    at the conventional path (resolved relative to the sidecar's
    working directory), otherwise `"default"`. The active block is
    the catastrophic-leak floor in either case when no YAML +
    explicit `--pii-block` is passed at serve time — the sidecar
    can't see what `--pii-block` flag the serve process was invoked
    with, so it reports the policy that WOULD be active if serve
    were restarted in the same working directory.

    Per-column verdict: each tag row carries an `effective_enforcement`
    field (one of `allowed` / `floor_blocked` / `blocked`). `blocked` =
    the column's category is in the operator's active policy block;
    `floor_blocked` = not in the operator's block, but caught by the
    always-on catastrophic-leak floor (enforced at every read gate —
    `describe_*` AND `get_metric`); `allowed` = neither.
    """
    from fastapi import HTTPException, Query

    from schemabrain.core.store import SQLiteStore
    from schemabrain.pii.categories import (
        CATASTROPHIC_LEAK_CATEGORIES,
        PII_CATEGORIES_ORDERED,
        PIICategory,
        Sensitivity,
    )
    from schemabrain.pii.policy import ColumnOverride, Policy
    from schemabrain.pii.policy_yaml import (
        PolicyYamlError,
        parse_policy_yaml_file,
        policy_to_yaml,
    )

    _POLICY_YAML_PATH = DEFAULT_POLICY_YAML_PATH
    # Bound the work a single preview request can ask for. The category
    # block set is naturally capped by the 12-value enum; column
    # overrides are operator-supplied and unbounded, so cap the list
    # length to keep the (untrusted, localhost) query from forcing an
    # arbitrarily large diff. Far above any realistic count of hand-
    # asserted dashboard overrides; the YAML-edit + `policy apply` path
    # carries no such cap, so a genuinely huge policy stays expressible.
    _MAX_PREVIEW_OVERRIDES = 1000

    # Repeatable query params for the preview route. Bound to singletons so
    # the `Query(...)` call isn't evaluated in the argument default (ruff
    # B008) — the empty-list default is FastAPI metadata, not a shared
    # mutable arg (FastAPI builds a fresh list per request).
    _BLOCK_QUERY = Query(default=[])
    _OVERRIDE_QUERY = Query(default=[])

    def _load_active_block() -> tuple[frozenset[PIICategory], str, str | None]:
        """(active_block, source_label, error_message)."""
        if not _POLICY_YAML_PATH.exists():
            return CATASTROPHIC_LEAK_CATEGORIES, "default", None
        try:
            policy = parse_policy_yaml_file(_POLICY_YAML_PATH)
        except PolicyYamlError as exc:
            return CATASTROPHIC_LEAK_CATEGORIES, "default", str(exc)
        return policy.block, "yaml", None

    @app.get("/api/pii/policy")
    def policy_route(source_connection_id: str | None = None) -> dict[str, Any]:
        active_block, block_source, yaml_error = _load_active_block()
        # Floor union is UNCONDITIONAL — even when active_block is the
        # empty frozenset (operator wrote `block: []` to suppress an
        # over-eager block), the catastrophic-leak triple is still
        # enforced at EVERY read gate (`describe_*` AND `get_metric` —
        # get_metric.py unions the same floor). Do NOT introduce an
        # `if not active_block` short-circuit here — pinned by
        # `test_policy_route_floor_holds_when_yaml_block_is_empty`.
        effective_block = active_block | CATASTROPHIC_LEAK_CATEGORIES

        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            rows = store.list_column_pii_tags_with_origin(source_connection_id=resolved_source)

        # Per-category roll-up: column count + entity count + sample
        # names + whether the active policy blocks the category. Iterated
        # in `PII_CATEGORIES_ORDERED` so the JSON key order is stable.
        category_columns: dict[str, list[tuple[str, str]]] = {
            cat: [] for cat in PII_CATEGORIES_ORDERED
        }
        per_column: list[dict[str, Any]] = []
        for qt, col, sens, cats, origin in rows:
            # `blocked` = the operator's active policy blocks this
            # category. `floor_blocked` = the operator's policy does NOT
            # list it, but the always-on catastrophic floor does — and
            # that floor is enforced everywhere, incl. get_metric, so the
            # column is genuinely blocked, not "describe-only". The split
            # is attribution (your editable policy vs the floor you can't
            # disable), not which tool enforces it.
            in_policy_block = bool(cats & active_block)
            in_effective_block = bool(cats & effective_block)
            if in_policy_block:
                verdict = "blocked"
            elif in_effective_block:
                verdict = "floor_blocked"
            else:
                verdict = "allowed"
            per_column.append(
                {
                    "qualified_table": qt,
                    "column_name": col,
                    "qualified_column": f"{qt}.{col}",
                    "sensitivity": sens,
                    "categories": sorted(cats),
                    "origin": origin,
                    "effective_enforcement": verdict,
                }
            )
            for cat in cats:
                if (
                    cat in category_columns
                ):  # pragma: no branch — cats is frozenset[PIICategory], same enum as category_columns keys
                    category_columns[cat].append((qt, col))

        category_rollup = []
        for cat in PII_CATEGORIES_ORDERED:
            cols = category_columns[cat]
            entities = sorted({qt for qt, _col in cols})
            samples = [f"{qt}.{col}" for qt, col in cols[:3]]
            category_rollup.append(
                {
                    "category": cat,
                    "column_count": len(cols),
                    "entity_count": len(entities),
                    "sample_columns": samples,
                    "blocked_by_active_policy": cat in active_block,
                    "blocked_by_catastrophic_floor": cat in CATASTROPHIC_LEAK_CATEGORIES,
                }
            )

        # Diff preview: how many columns would each posture change?
        # "all" (every PIICategory blocks); "catastrophic_only" (just
        # the always-on floor); "none" (empty operator block — but the
        # floor still blocks its columns at every read gate). Computed
        # from per-column intersections so the operator sees the concrete
        # impact of switching postures. `current_blocked` counts only
        # columns in the operator's ACTIVE POLICY block; floor-only
        # columns surface separately as `if_catastrophic_only`.
        all_block: frozenset[PIICategory] = frozenset(PII_CATEGORIES_ORDERED)
        current_blocked = sum(
            1 for entry in per_column if entry["effective_enforcement"] == "blocked"
        )
        diff_preview = {
            "current_blocked": current_blocked,
            "if_all_blocked": sum(
                1 for entry in per_column if set(entry["categories"]) & all_block
            ),
            "if_catastrophic_only": sum(
                1 for entry in per_column if set(entry["categories"]) & CATASTROPHIC_LEAK_CATEGORIES
            ),
            "if_none_blocked": 0,
        }

        return {
            "source_connection_id": resolved_source,
            "policy_path": str(_POLICY_YAML_PATH),
            "block_source": block_source,
            "active_block": sorted(active_block),
            "catastrophic_floor": sorted(CATASTROPHIC_LEAK_CATEGORIES),
            "effective_block_for_describe": sorted(effective_block),
            "category_rollup": category_rollup,
            "per_column": per_column,
            "diff_preview": diff_preview,
            "yaml_parse_error": yaml_error,
            "policy_drift": _compute_policy_drift(_POLICY_YAML_PATH),
        }

    def _load_current_policy() -> tuple[Policy, str | None]:
        """(current_policy, parse_error). Mirrors `_load_active_block` but
        returns the whole `Policy` (block + column_overrides + description)
        so the preview can diff staged YAML against a faithful canonical
        rendering of what's on disk. Falls back to the catastrophic-floor
        default when no YAML exists or the file fails to parse — same
        posture as `_load_active_block`, so the diff baseline matches what
        the running firewall would actually enforce.
        """
        if not _POLICY_YAML_PATH.exists():
            return Policy(block=CATASTROPHIC_LEAK_CATEGORIES), None
        try:
            return parse_policy_yaml_file(_POLICY_YAML_PATH), None
        except PolicyYamlError as exc:
            return Policy(block=CATASTROPHIC_LEAK_CATEGORIES), str(exc)

    def _parse_override_param(raw: str) -> ColumnOverride:
        """Decode one `override` query value into a `ColumnOverride`.

        Wire shape: ``<qualified_column>|<sensitivity>|<comma-joined
        categories>`` — e.g. ``public.users.email|internal|`` for the
        canonical "mark safe" (empty categories) override, or
        ``s.t.c|pii|contact,location`` for an explicit reclassification.
        `ColumnOverride.__post_init__` validates the column shape,
        sensitivity, and categories; any violation surfaces as a 400.
        """
        parts = raw.split("|")
        if len(parts) != 3:
            raise HTTPException(
                400,
                detail=(
                    f"override must be 'qualified_column|sensitivity|categories' (got {raw!r})"
                ),
            )
        qualified_column, sensitivity, categories_csv = parts
        categories = frozenset(c for c in categories_csv.split(",") if c)
        try:
            return ColumnOverride(
                qualified_column=qualified_column,
                sensitivity=cast(Sensitivity, sensitivity),
                categories=cast("frozenset[PIICategory]", categories),
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    def _yaml_diff(current_yaml: str, staged_yaml: str) -> list[dict[str, str]]:
        """Line-level diff of two canonical YAML bodies as ordered
        ``{kind, text}`` rows (``context`` / ``remove`` / ``add``). The
        staged-diff pane renders these directly; `ndiff` hint lines
        (``? ``) are dropped.
        """
        out: list[dict[str, str]] = []
        for line in difflib.ndiff(current_yaml.splitlines(), staged_yaml.splitlines()):
            tag, text = line[:2], line[2:]
            if tag == "  ":
                out.append({"kind": "context", "text": text})
            elif tag == "- ":
                out.append({"kind": "remove", "text": text})
            elif tag == "+ ":
                out.append({"kind": "add", "text": text})
        return out

    @app.get("/api/pii/policy/preview")
    def policy_preview_route(
        block: list[str] = _BLOCK_QUERY,
        override: list[str] = _OVERRIDE_QUERY,
    ) -> dict[str, Any]:
        """Render the canonical YAML + line diff for a *staged* policy.

        Pure, side-effect-free renderer (ADR 0006): the dashboard posts
        its staged block set + column overrides as repeatable query
        params, and this returns ``policy_to_yaml(staged)`` plus the line
        diff against the current on-disk policy. It writes nothing and
        reads no per-source store state — qualified columns are global
        strings, so the rendering is source-independent.

        The catastrophic-downgrade guard (`CatastrophicDowngradeError`)
        is NOT simulated here — that's enforced at `policy apply` time by
        the store. The dashboard never offers a floor-column override
        (ADR 0007), so a downgrade can't be staged through the UI; a YAML
        that would strip a floor protection still renders, and the
        operator's own `policy apply` rejects it loudly.
        """
        if len(override) > _MAX_PREVIEW_OVERRIDES:
            raise HTTPException(
                400,
                detail=f"too many overrides (max {_MAX_PREVIEW_OVERRIDES}, got {len(override)})",
            )

        current_policy, current_parse_error = _load_current_policy()
        overrides = tuple(_parse_override_param(raw) for raw in override)
        try:
            staged_policy = Policy(
                block=cast("frozenset[PIICategory]", frozenset(block)),
                column_overrides=overrides,
                # Carry the on-disk description so the diff doesn't show a
                # spurious description add/drop the operator never made.
                description=current_policy.description,
            )
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

        current_yaml = policy_to_yaml(current_policy)
        staged_yaml = policy_to_yaml(staged_policy)
        return {
            "staged_yaml": staged_yaml,
            "current_yaml": current_yaml,
            "diff_lines": _yaml_diff(current_yaml, staged_yaml),
            "changed": staged_yaml != current_yaml,
            "staged_block": sorted(staged_policy.block),
            "current_parse_error": current_parse_error,
        }


def _register_drift_route(app: FastAPI, config: SidecarConfig) -> None:
    """GET /api/drift — read-only context-health signal.

    Surfaces the two drift kinds the store-only sidecar can honestly
    verify from persisted state alone:

      - ``config``: the policy YAML diverged from what ``serve`` recorded
        at boot (reuses ``_compute_policy_drift``).
      - ``enrichment``: a persisted column description was generated with
        an older prompt version than the live one, so the AI context is
        stale (reuses ``store.has_stale_prompt_version``).

    SCHEMA drift (table/column missing) and YAML-vs-disk DEFINITION drift
    are DELIBERATELY OMITTED: both require a live DB connection the
    credential-less sidecar does not have, so emitting them would be
    fabricated. The surface stays honest — ``fresh`` is true iff there
    are zero items. Every action is a copy-the-CLI-command affordance
    (ADR-0006); the route writes nothing and never flips state.
    """
    from schemabrain.core.store import SQLiteStore
    from schemabrain.enrichment.prompts import PROMPT_VERSION

    @app.get("/api/drift")
    def drift(source_connection_id: str | None = None) -> dict[str, Any]:
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            # Coerce a URL-shaped override to its canonical hashed id BEFORE
            # it touches the store query or the response — never echo a raw
            # connection URL (it carries the DB password). A hashed id /
            # label passes through unchanged, so the normal path is a no-op.
            safe_source = _credential_safe_source_label(resolved_source)
            last_enriched = store.latest_enriched_at(source_connection_id=safe_source)
            enrichment_stale = store.has_stale_prompt_version(
                source_connection_id=safe_source,
                current_prompt_version=PROMPT_VERSION,
            )
            indexed_by_source = {
                s.source_connection_id: s.last_indexed_at for s in store.summarize_sources()
            }

        items: list[dict[str, Any]] = []

        # --- config drift: policy YAML edited since `serve` recorded it ---
        # Global (not per-source): the policy file governs every source.
        policy_drift = _compute_policy_drift(DEFAULT_POLICY_YAML_PATH)
        if policy_drift["detected"]:
            items.append(
                {
                    "kind": "config",
                    "risk": "med",
                    "title": "Policy file changed since serve started",
                    "when": policy_drift["current_mtime"],
                    "detail": (
                        "pii_policy.yaml was edited after `schemabrain serve` recorded it. "
                        "The running firewall still enforces the policy it loaded at boot — "
                        "restart serve to apply the change."
                    ),
                    "action": {"label": "Copy restart command", "command": "schemabrain serve"},
                }
            )

        # --- enrichment drift: descriptions built on an older prompt ---
        if enrichment_stale:
            items.append(
                {
                    "kind": "enrichment",
                    "risk": "med",
                    "title": "AI context built on an older prompt version",
                    "when": None,
                    "detail": (
                        f"Some column descriptions predate the live prompt version "
                        f"({PROMPT_VERSION}). A structural re-index will not refresh them — "
                        f"re-enrich to regenerate the descriptions with the current prompt."
                    ),
                    "action": {
                        "label": "Copy re-enrich command",
                        "command": "schemabrain index <source> --enrich",
                    },
                }
            )

        return {
            "source_connection_id": safe_source,
            "freshness": {
                "fresh": len(items) == 0,
                "change_count": len(items),
                # Every drift kind is "med" today; high_risk is a forward-
                # compatible UI-contract field (the hero shows a high-risk
                # count) that stays 0 until a high-risk kind ships.
                "high_risk": sum(1 for item in items if item["risk"] == "high"),
                # Epoch seconds; None when nothing enriched / no tables —
                # the UI renders "—", never a fabricated 0.
                "last_enriched": last_enriched,
                "last_indexed": indexed_by_source.get(safe_source),
                # Credential-safe: a URL-shaped source is hashed to its
                # canonical id by _credential_safe_source_label before it
                # reaches here, so this never carries the raw URL.
                "source_label": safe_source,
                "prompt_version": PROMPT_VERSION,
            },
            "items": items,
        }


def _register_graph_route(app: FastAPI, config: SidecarConfig) -> None:
    """GET /api/graph — read-only knowledge-graph projection (ADR 0010).

    Serves the persisted ``graph_nodes`` / ``graph_edges`` read-model: one
    node per entity (cosmetic ``group`` + cached ``row_count``), one edge
    per canonical join with honest ``evidence`` (declared FK / log-mined /
    inferred — never inspected SQL) and, for declared FK edges only, the
    equi-join ``cardinality`` (v16; null for mined/inferred so an
    unverified shape never reads as engine-derived), plus the ordered
    canonical path (the diameter spine) reconstructed from the rank-1 edges.

    ``pii_level`` is recomputed LIVE per node from the current PII tags —
    via the SAME ``_entity_pii_level`` helper the PII matrix uses — so the
    graph never disagrees with /pii, even after a tag override the last
    projection did not see. It is the full 5-state severity
    (catastrophic / pii / confidential / internal / none), not a collapsed
    boolean, so the surface renders the catastrophic alarm and the lighter
    halo from one source of truth.

    ``refusal_count`` per node and the response's ``unattributed_refusals``
    are a LIVE tally of append-only ``mcp_audit`` rows with
    ``status='refused'``, grouped by the non-canonical ``anchor_entity``
    column (store v17) — never persisted to the projection (refusals accrue
    between rebuilds, so a snapshot would go stale). The split is honest and
    EXHAUSTIVE: a refusal whose ``anchor_entity`` is a current entity
    increments that node's count; one with a NULL anchor OR an anchor that is
    no longer a current entity (renamed/dropped since the immutable row was
    logged) increments ``unattributed_refusals`` instead. So the per-node
    counts PLUS ``unattributed_refusals`` always equal the log's refused-row
    count — a refusal is never silently dropped or misattributed. Pure read —
    the chained audit columns are never touched.

    A source that exists but has no projection yields empty arrays at 200; no
    resolvable source is a 409. The route writes nothing.
    """
    from schemabrain.core.store import SQLiteStore
    from schemabrain.pii.categories import CATASTROPHIC_LEAK_CATEGORIES

    @app.get("/api/graph")
    def graph(source_connection_id: str | None = None) -> dict[str, Any]:
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            # Hash a URL-shaped override to its canonical id BEFORE it
            # touches the store query or the response — never echo a raw
            # connection URL (it carries the DB password).
            safe_source = _credential_safe_source_label(resolved_source)
            graph_nodes = store.list_graph_nodes(source_connection_id=safe_source)
            graph_edges = store.list_graph_edges(source_connection_id=safe_source)
            entities_by_name = {
                entity.name: entity
                for entity in store.list_entities(source_connection_id=safe_source)
            }
            # LIVE refusal tally for the refusal-hotspot overlay (PR-17b).
            # Aggregated per request from the append-only `mcp_audit` log
            # (status='refused', grouped by the non-canonical `anchor_entity`
            # column) — NOT persisted to the projection, since refusals accrue
            # between rebuilds and a snapshot would go stale immediately. Reads
            # only; the chained columns are never touched.
            hotspots = store.refusal_counts_by_entity(source_connection_id=safe_source)
            # Reconciliation guard: a refusal can carry an `anchor_entity` that
            # is no longer a current entity — the row is immutable (append-only)
            # but the entity may have been renamed/dropped and the projection
            # rebuilt since. Such an anchor matches no node, so its count would
            # be shown nowhere. Fold those "orphaned" refusals into the
            # unattributed remainder so the per-node badges PLUS this figure
            # always equal the audit log's refused-row count — never silently
            # dropped (the honesty guarantee the surface asserts).
            node_names = {node.entity_name for node in graph_nodes}
            orphaned_refusals = sum(
                count for name, count in hotspots.by_entity.items() if name not in node_names
            )
            unattributed_refusals = hotspots.unattributed + orphaned_refusals
            nodes = [
                {
                    "id": node.entity_name,
                    "label": node.entity_name,
                    "group": node.group,
                    # LIVE PII-severity overlay: the SAME `_entity_pii_level`
                    # the PII matrix uses, so the graph's floor can never
                    # drift from /pii. The node→entity FK guarantees the
                    # lookup hits. Emitting the full 5-state level (not a
                    # collapsed boolean) lets the surface render the
                    # catastrophic alarm AND the lighter non-catastrophic
                    # halo from ONE source of truth — never a second
                    # client-side re-derivation of catastrophic-ness.
                    "pii_level": _entity_pii_level(
                        store,
                        entities_by_name[node.entity_name],
                        source_connection_id=safe_source,
                        catastrophic_categories=CATASTROPHIC_LEAK_CATEGORIES,
                    ),
                    "row_count": node.row_count,
                    # LIVE per-entity refusal count (0 when none). Honest
                    # aggregate of attributed `mcp_audit` refusals; entities
                    # the engine could not attribute fall into the response's
                    # top-level `unattributed_refusals` instead — never
                    # silently folded into a node's count.
                    "refusal_count": hotspots.by_entity.get(node.entity_name, 0),
                }
                for node in graph_nodes
            ]

        edges = [
            {
                "id": edge.join_name,
                "source": edge.source_entity,
                "target": edge.target_entity,
                "evidence": edge.edge_origin,
                "canonical_path_rank": edge.canonical_path_rank,
                # Equi-join shape, present ONLY on declared FK edges (the
                # projection writer drops it for log_mined / inferred so an
                # unverified shape never reads as engine-derived); null
                # otherwise — never a guess. See ADR 0011.
                "cardinality": edge.cardinality,
            }
            for edge in graph_edges
        ]
        return {
            "source_connection_id": safe_source,
            "nodes": nodes,
            "edges": edges,
            "canonical_path": _reconstruct_canonical_path(graph_edges),
            # Refused `mcp_audit` rows not shown on any visible node: a NULL
            # `anchor_entity` (the engine could not pin it to one entity) OR an
            # orphaned anchor naming an entity no longer in the projection.
            # Surfaced explicitly so the badges + this figure always reconcile
            # with the audit log — a refusal is never silently dropped.
            "unattributed_refusals": unattributed_refusals,
        }


def _register_stream_route(app: FastAPI, config: SidecarConfig) -> None:
    """GET /api/audit/stream — SSE feed of new audit rows."""
    from sse_starlette.sse import EventSourceResponse
    from starlette.requests import Request

    @app.get("/api/audit/stream")
    async def audit_stream_route(request: Request, since_id: int = 0) -> EventSourceResponse:
        # SSE generator body is exercised by the manual smoke
        # (`scripts/dashboard_demo.py` → browser navigation triggers
        # the EventSource); a unit-level async test would need a
        # long-running fixture + sse-starlette client, both of which
        # add brittleness disproportionate to the value vs the manual
        # smoke. The route-registration test in
        # `tests/dashboard/test_sidecar_routes.py::test_sse_stream_route_exists`
        # asserts the surface exists; this body covers the wire
        # protocol detail.
        async def event_generator() -> Any:  # pragma: no cover
            last_seen = since_id
            while True:
                if await request.is_disconnected():
                    return
                with _open_audit_conn(config) as conn:
                    new_rows = conn.execute(
                        "SELECT id, occurred_at, tool_name, status, "
                        "refusal_reason, cost_class, pii_categories, "
                        "chain_hash FROM mcp_audit WHERE id > ? "
                        "ORDER BY id ASC LIMIT 50",
                        (last_seen,),
                    ).fetchall()
                for row in new_rows:
                    payload = {
                        "id": row["id"],
                        "occurred_at": row["occurred_at"],
                        "tool_name": row["tool_name"],
                        "status": row["status"],
                        "refusal_reason": row["refusal_reason"],
                        "cost_class": row["cost_class"],
                        "pii_categories": row["pii_categories"],
                        "chain_hash_hex": bytes(row["chain_hash"]).hex(),
                    }
                    yield {"event": "audit_row", "data": json.dumps(payload)}
                    last_seen = row["id"]
                await asyncio.sleep(SSE_TICK_SECONDS)

        return EventSourceResponse(event_generator())  # pragma: no cover


# -----------------------------------------------------------------------------
# Helpers — shared across route registrants.
# -----------------------------------------------------------------------------


def _open_audit_conn(config: SidecarConfig) -> sqlite3.Connection:
    """Open a read-side SQLite connection against the audit DB.

    The sidecar never writes; the writer-discipline contract is
    enforced by code path (no INSERT/UPDATE/DELETE statements anywhere
    in this module) and belt-and-suspenders by ``PRAGMA query_only``.
    The store is already in WAL mode (set by the writer in
    ``core/store.py``); this read connection inherits it and so sees rows
    committed by a concurrent ``schemabrain serve`` process.
    """
    conn = sqlite3.connect(str(config.store_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _resolve_source(store: Any, config: SidecarConfig, override: str | None) -> str:
    """Pick the source_connection_id to use for entity queries.

    Precedence: explicit query param > config default > first source
    in the store. Raises ValueError if the store has no sources.
    """
    if override:
        return override
    if config.source_connection_id:
        return config.source_connection_id
    sources = store.list_distinct_source_connection_ids()
    if not sources:
        raise _no_sources_http_error()
    return sources[0]


def _no_sources_http_error() -> Exception:
    from fastapi import HTTPException

    return HTTPException(
        status_code=409,
        detail=(
            "store has no indexed sources. Run `schemabrain index` against your source DB first."
        ),
    )


def _credential_safe_source_label(source: str) -> str:
    """Coerce a resolved source id to a form safe to echo over HTTP.

    ``_resolve_source`` returns an operator-supplied ``source_connection_id``
    override verbatim, so a URL-shaped value (which carries the DB password)
    could otherwise reach the response body. Hash any URL-shaped source to
    the canonical short id — the same id indexing persisted — before it is
    emitted, mirroring the defence in ``_register_meta_route``. A normal
    hashed id / label is not URL-shaped and passes through unchanged.
    """
    from schemabrain.core.source_id import looks_like_connection_url, make_source_id

    if looks_like_connection_url(source):
        try:
            return make_source_id(source)
        except ValueError:  # pragma: no cover - defensive: looks_like_connection_url already validated the Postgres scheme make_source_id needs, so it cannot raise; we refuse to echo a partial URL rather than leak it
            return "source"
    return source


def _validate_pagination(limit: int, offset: int) -> tuple[int, int]:
    from fastapi import HTTPException

    if limit < 1 or limit > 500:
        raise HTTPException(400, detail="limit must be 1..500")
    if offset < 0:
        raise HTTPException(400, detail="offset must be >= 0")
    return limit, offset


def _maybe_resolve_since(conn: sqlite3.Connection, since: str | None) -> int | None:
    from fastapi import HTTPException

    from schemabrain.audit.verify import SinceCursorError, resolve_since_cursor

    if since is None:
        return None
    try:
        return resolve_since_cursor(conn, since)
    except (SinceCursorError, ValueError) as exc:
        raise HTTPException(400, detail=str(exc)) from exc


def _select_audit_rows(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    after_id: int | None,
    status_filter: str | None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    if status_filter is not None:
        clauses.append("status = ?")
        params.append(status_filter)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    params.extend([limit, offset])
    sql = (
        f"SELECT * FROM mcp_audit {where} "  # nosec B608 - clauses come from typed branches above
        f"ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, tuple(params)).fetchall()


def _count_audit_rows(
    conn: sqlite3.Connection,
    *,
    after_id: int | None,
    status_filter: str | None,
) -> int:
    clauses: list[str] = []
    params: list[Any] = []
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)
    if status_filter is not None:
        clauses.append("status = ?")
        params.append(status_filter)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    sql = f"SELECT count(*) AS c FROM mcp_audit {where}"  # nosec B608
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row["c"]) if row else 0


def _serialize_audit_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert an ``mcp_audit`` SQLite Row to the JSON shape the UI consumes."""
    return {
        "id": row["id"],
        "occurred_at": row["occurred_at"],
        "source_connection_id": row["source_connection_id"],
        "caller_id": row["caller_id"],
        "tool_name": row["tool_name"],
        "status": row["status"],
        "refusal_reason": row["refusal_reason"],
        "cost_class": row["cost_class"],
        "pii_categories": (row["pii_categories"].split(",") if row["pii_categories"] else []),
        "ast_shape_hash_hex": (
            bytes(row["ast_shape_hash"]).hex() if row["ast_shape_hash"] else None
        ),
        "rule_id": row["rule_id"],
        "fingerprint_hex": bytes(row["fingerprint"]).hex(),
        "fingerprint_version": row["fingerprint_version"],
        "chain_hash_hex": bytes(row["chain_hash"]).hex(),
    }


def _serialize_refusal(row: sqlite3.Row, *, include_envelope: bool = False) -> dict[str, Any]:
    """Augment a refused audit row with a reconstructed envelope view.

    The envelope is rebuilt from the row's structured columns (the
    historical full envelope is not persisted by design — see
    privacy-by-construction in ``audit/fingerprint.py``). The shape
    matches ``ToolResponse`` close enough for the UI to render the
    same fields it would have shown live.
    """
    base = _serialize_audit_row(row)
    base["envelope_reconstructed"] = include_envelope
    if include_envelope:
        pii_cats = row["pii_categories"].split(",") if row["pii_categories"] else []
        base["envelope"] = {
            "status": "refused",
            "data": None,
            "confidence": None,
            "error": {
                "kind": row["refusal_reason"] or "pii_blocked",
                "message": _refusal_message(row["refusal_reason"], pii_cats),
                "pii_categories": pii_cats,
                "recovery": {
                    "suggested_tool": None,
                    "suggested_args": None,
                    "fuzzy_matches": [],
                    "suggested_rewrite": None,
                    "widening_hint": None,
                },
            },
            "follow_up_hints": None,
            "degradation_reason": None,
            "charter_version": "1.2",
        }
    return base


def _refusal_message(reason: str | None, pii_categories: list[str]) -> str:
    """Synthesize a human refusal sentence from the structured columns."""
    if reason == "pii_blocked" and pii_categories:
        return (
            f"Refused: tool call touched PII categories "
            f"{sorted(pii_categories)}. Use the recovery hint or widen "
            f"the allowlist."
        )
    if reason == "pii_blocked":
        return "Refused: tool call would touch a blocked PII category."
    if reason == "allowlist_violation":
        return "Refused: tool call falls outside the operator's allowlist scope."
    return f"Refused: {reason or 'reason_unknown'}."


def _register_html_fallback(app: FastAPI) -> None:
    """Rewrite extensionless GET requests to ``<path>.html`` when the
    file exists on disk.

    Bridges the gap between Next.js's static-export filename convention
    (``pii.html``) and the React app's URL convention (``/pii``). Only
    fires on GET, only for paths outside ``/api/...``, and only when
    the ``.html`` sibling actually exists — so a real 404 (typo,
    deleted route) still returns 404 cleanly.

    Implemented as middleware that runs BEFORE the static mount so the
    mount sees the rewritten path. The static mount then serves the
    bytes the normal way.
    """
    from starlette.requests import Request
    from starlette.responses import FileResponse, Response

    @app.middleware("http")
    async def html_fallback(request: Request, call_next: Any) -> Response:
        path = request.url.path
        # Pre-call rewrites only for plain GETs to non-/api extensionless paths.
        if (
            request.method == "GET"
            and not path.startswith("/api/")
            and "." not in path.rsplit("/", 1)[-1]
            and path not in ("/", "")
        ):
            candidate = STATIC_DIR / f"{path.lstrip('/')}.html"
            if candidate.is_file():
                return FileResponse(
                    candidate,
                    media_type="text/html; charset=utf-8",
                    headers={
                        "X-Schemabrain-Charter-Version": "1.2",
                        "X-Schemabrain-Dashboard-Schema": DASHBOARD_SCHEMA_VERSION,
                    },
                )
        return await call_next(request)


def _register_fallback_landing(app: FastAPI) -> None:
    """Serve a minimal landing page at ``/`` when no static export exists.

    The page is dev-mode only — it renders when the wheel has not
    bundled the Next.js export and when the operator is not running
    ``pnpm dev`` separately. It fetches live data from the /api/*
    routes and shows the operator a real surface so the sidecar feels
    "alive" the moment it boots, instead of returning 404 at /.

    Inline HTML on purpose: no Jinja, no template files. The full
    React UI is the production surface; this is just a working
    fallback for D1 contributors and a "the sidecar is alive" probe.
    """
    from starlette.responses import HTMLResponse

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing() -> str:
        return _LANDING_HTML


_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SchemaBrain dashboard — sidecar</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --ink-900: oklch(18% 0 0);
    --ink-500: oklch(58% 0 0);
    --ink-200: oklch(88% 0 0);
    --ivory: oklch(98% 0.005 80);
    --signal-green: oklch(68% 0.16 145);
    --signal-amber: oklch(78% 0.16 75);
    --signal-red: oklch(62% 0.22 25);
    --mono: ui-monospace, "JetBrains Mono", monospace;
    --body: ui-sans-serif, system-ui, -apple-system, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: var(--body);
    background: var(--ivory);
    color: var(--ink-900);
    line-height: 1.5;
  }
  .wrap { max-width: 64rem; margin: 0 auto; padding: 4rem 2rem; }
  .eyebrow {
    font-family: var(--mono); font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.15em;
    color: var(--ink-500);
  }
  h1 {
    font-size: clamp(2rem, 1rem + 3vw, 3.5rem);
    line-height: 1.05; margin: 0.75rem 0 1rem;
  }
  .lede { color: var(--ink-500); max-width: 40rem; }
  .banner {
    margin: 2.5rem 0;
    padding: 1rem 1.25rem;
    border-left: 3px solid var(--signal-amber);
    background: oklch(96% 0.005 80);
    font-family: var(--mono); font-size: 0.875rem;
  }
  .banner strong { color: var(--ink-900); }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 1rem;
    margin: 1rem 0 3rem;
  }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
  .stat {
    border: 1px solid var(--ink-200);
    padding: 1.25rem;
    background: white;
  }
  .stat .label {
    font-family: var(--mono); font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.1em;
    color: var(--ink-500);
  }
  .stat .value {
    font-size: 2rem;
    font-weight: 600;
    margin-top: 0.5rem;
    font-variant-numeric: tabular-nums;
  }
  .stat .value.ok { color: var(--signal-green); }
  .stat .value.warn { color: var(--signal-amber); }
  .stat .value.err { color: var(--signal-red); }
  h2 {
    font-size: 1.125rem;
    margin: 2.5rem 0 1rem;
    font-family: var(--mono);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-500);
  }
  ul.routes { list-style: none; padding: 0; margin: 0; }
  ul.routes li {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--ink-200);
    display: flex; justify-content: space-between; align-items: baseline;
  }
  ul.routes li:hover { background: white; }
  ul.routes a {
    font-family: var(--mono);
    color: var(--ink-900);
    text-decoration: none;
  }
  ul.routes a:hover { color: var(--signal-green); }
  ul.routes span { color: var(--ink-500); font-size: 0.875rem; }
  code { font-family: var(--mono); background: white; padding: 0.1em 0.3em; border: 1px solid var(--ink-200); }
</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">SchemaBrain · v0.4 · sidecar (dev fallback)</p>
  <h1>The sidecar is running.</h1>
  <p class="lede">
    This is a minimal landing page served by the FastAPI sidecar when
    no Next.js static export is bundled. The real React UI is built
    separately — see "Get the full UI" below.
  </p>

  <div class="banner">
    <strong>Why am I seeing this page?</strong> The wheel does not yet
    ship a built Next.js export under <code>schemabrain/dashboard/static/</code>.
    The sidecar still serves all of its API routes; you can browse them
    below.
  </div>

  <h2>Live data</h2>
  <div class="grid">
    <div class="stat">
      <div class="label">Entities indexed</div>
      <div class="value" id="stat-entities">…</div>
    </div>
    <div class="stat">
      <div class="label">Audit rows</div>
      <div class="value" id="stat-rows">…</div>
    </div>
    <div class="stat">
      <div class="label">Chain status</div>
      <div class="value" id="stat-chain">…</div>
    </div>
  </div>

  <h2>API routes</h2>
  <ul class="routes">
    <li><a href="/api/health">/api/health</a><span>liveness + store probe</span></li>
    <li><a href="/api/meta">/api/meta</a><span>charter + sidecar info</span></li>
    <li><a href="/api/entities">/api/entities</a><span>PII Viz matrix data</span></li>
    <li><a href="/api/audit/rows">/api/audit/rows</a><span>Audit Viewer table</span></li>
    <li><a href="/api/audit/refusals">/api/audit/refusals</a><span>Refusal Experience feed</span></li>
    <li><a href="/api/audit/verify">/api/audit/verify</a><span>chain verify status</span></li>
    <li><a href="/api/audit/merkle/root">/api/audit/merkle/root</a><span>derived Merkle root</span></li>
  </ul>

  <h2>Get the full UI</h2>
  <p class="lede">
    Two paths:
  </p>
  <ul class="routes">
    <li>
      <code>cd web/ && pnpm install && pnpm dev</code>
      <span>Next.js dev server at :3000, proxies /api to this sidecar</span>
    </li>
    <li>
      <code>cd web/ && pnpm build && pnpm export</code>
      <span>builds the static export into schemabrain/dashboard/static/</span>
    </li>
  </ul>
</div>

<script>
  async function fetchStats() {
    try {
      const [entities, rows, verify] = await Promise.all([
        fetch("/api/entities").then(r => r.ok ? r.json() : null).catch(() => null),
        fetch("/api/audit/rows").then(r => r.json()),
        fetch("/api/audit/verify").then(r => r.json()),
      ]);
      document.getElementById("stat-entities").textContent =
        entities ? entities.count : "—";
      document.getElementById("stat-rows").textContent = rows.total;
      const chainEl = document.getElementById("stat-chain");
      chainEl.textContent = verify.status;
      chainEl.classList.add(verify.status === "intact" ? "ok" : "err");
    } catch (err) {
      console.error(err);
    }
  }
  fetchStats();
</script>
</body>
</html>
"""


def assert_route_table_is_read_only(app: Any) -> None:
    """Invariant check: every route must use a read-only HTTP method.

    Called by ``tests/dashboard/test_invariants.py`` against a freshly
    constructed app. The implementation walks ``app.routes`` and
    rejects any route whose ``methods`` set includes ``POST`` /
    ``PUT`` / ``PATCH`` / ``DELETE``. ``HEAD`` and ``OPTIONS`` are
    allowed (they don't mutate). Defined here (not in tests) so the
    same check can run in a future deploy smoke without copying logic.
    """
    forbidden = {"POST", "PUT", "PATCH", "DELETE"}
    for route in getattr(app, "routes", []):
        methods = getattr(route, "methods", None) or set()
        violated = methods & forbidden
        if violated:
            raise AssertionError(
                f"dashboard sidecar must be read-only; route "
                f"{getattr(route, 'path', '?')} declares {sorted(violated)}"
            )


__all__ = [
    "BIND_HOST",
    "CHARTER_VERSION_HEADER",
    "DEFAULT_PORT",
    "SSE_TICK_SECONDS",
    "SidecarConfig",
    "assert_route_table_is_read_only",
    "create_sidecar",
    "is_ui_available",
]
