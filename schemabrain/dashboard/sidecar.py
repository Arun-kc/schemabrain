"""FastAPI sidecar for the v0.4 dashboard.

The sidecar exposes 8 read-only JSON routes + 1 SSE stream + 1 health
endpoint, plus serves the static Next.js export at ``/``. Architectural
invariants:

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
  SSE.   GET  /api/audit/stream                live audit row push
  H.     GET  /api/health                      liveness
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

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

# SSE tick interval — matches the 2s polling story in RFC §6
# (deferred-items log). Real-time push is a v0.5 V5-2 item.
SSE_TICK_SECONDS: float = 2.0


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

    return app


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
        # Coerce config.source_connection_id to a credential-safe form
        # before echoing it back. If it's a connection URL, hash it to
        # the canonical short ID; otherwise treat it as a pre-computed
        # label and pass through. Never emit the raw URL — it carries
        # the DB password.
        configured = config.source_connection_id
        if configured and looks_like_connection_url(configured):
            try:
                configured = make_source_id(configured)
            except ValueError:
                # Unparseable URL — refuse to echo it back rather than
                # leak partial credentials. Fall through to the store's
                # indexed list.
                configured = None
        return {
            "charter_version": CHARTER_VERSION,
            "dashboard_schema_version": DASHBOARD_SCHEMA_VERSION,
            "fingerprint_version": FINGERPRINT_VERSION,
            "store_path": str(config.store_path),
            "default_source_connection_id": (configured or (source_ids[0] if source_ids else None)),
            "source_connection_ids": source_ids,
        }


def _register_entity_routes(app: FastAPI, config: SidecarConfig) -> None:
    """PII Viz routes — list entities + per-entity column drill-down."""
    from fastapi import HTTPException

    from schemabrain.core.store import SQLiteStore
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
                    if tag is None:
                        total_internal_or_public += 1
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
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            entities = store.list_entities(source_connection_id=resolved_source)
        items = [
            {
                "name": e.name,
                "description": e.description,
                "qualified_table": e.qualified_table,
                "identity": e.identity,
                "origin": e.origin,
                "inference_method": e.inference_method,
                "validation_state": e.validation_state,
            }
            for e in entities
        ]
        return {
            "source_connection_id": resolved_source,
            "items": items,
            "count": len(items),
        }

    @app.get("/api/entities/{name}/columns")
    def entity_columns_route(name: str, source_connection_id: str | None = None) -> dict[str, Any]:
        with SQLiteStore(config.store_path) as store:
            resolved_source = _resolve_source(store, config, source_connection_id)
            entity = store.get_entity(name, source_connection_id=resolved_source)
            if entity is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"entity {name!r} not found for source {resolved_source!r}",
                )
            schema_name, table_name = entity.qualified_table.split(".", 1)
            table = store.get_table(schema_name, table_name, source_connection_id=resolved_source)
            if table is None:  # pragma: no cover - defensive
                # An entity bound to a table that's no longer indexed —
                # ship the entity with zero columns rather than 500.
                # Unreachable under the store's FK guarantee
                # (`delete_table` cascades to entities), but kept as a
                # belt-and-suspenders guard against a future store
                # backend that breaks the cascade.
                column_names: list[str] = []
                pii_tags: dict[str, Any] = {}
            else:
                column_names = [c.name for c in table.columns]
                raw_tags = store.get_column_pii_tags(
                    source_connection_id=resolved_source,
                    qualified_table=entity.qualified_table,
                    columns=column_names,
                )
                pii_tags = {
                    col: {
                        "sensitivity": tag[0],
                        "pii_categories": sorted(tag[1]),
                    }
                    for col, tag in raw_tags.items()
                }

            # Fetch active metrics and joins anchored on this entity inside the with-block
            all_metrics = store.list_metrics(source_connection_id=resolved_source)
            all_joins = store.list_canonical_joins(source_connection_id=resolved_source)

        columns = [
            {
                "name": col,
                "sensitivity": pii_tags.get(col, {}).get("sensitivity", "public"),
                "pii_categories": pii_tags.get(col, {}).get("pii_categories", []),
            }
            for col in column_names
        ]

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

        joins = [
            {
                "name": j.name,
                "description": j.description,
                "source_entity": j.source_entity,
                "target_entity": j.target_entity,
                "on": [
                    {"source_column": edge.source_column, "target_column": edge.target_column}
                    for edge in j.on
                ],
            }
            for j in all_joins
            if j.source_entity == name or j.target_entity == name
        ]

        return {
            "entity": {
                "name": entity.name,
                "qualified_table": entity.qualified_table,
                "identity": entity.identity,
                "origin": entity.origin,
                "inference_method": entity.inference_method,
                "validation_state": entity.validation_state,
            },
            "columns": columns,
            "metrics": metrics,
            "joins": joins,
        }


def _register_audit_routes(app: FastAPI, config: SidecarConfig) -> None:
    """Audit routes — rows, refusals, verify."""
    from fastapi import HTTPException

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
    field (one of `allowed` / `describe_blocked` / `blocked`) computed
    from the column's categories intersected with (active_block | catastrophic_floor).
    """
    from schemabrain.core.store import SQLiteStore
    from schemabrain.pii.categories import (
        CATASTROPHIC_LEAK_CATEGORIES,
        PII_CATEGORIES_ORDERED,
        PIICategory,
    )
    from schemabrain.pii.policy_yaml import (
        PolicyYamlError,
        parse_policy_yaml_file,
    )

    _POLICY_YAML_PATH = Path("./schemabrain/pii_policy.yaml")

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
            in_metric_block = bool(cats & active_block)
            in_describe_block = bool(cats & effective_block)
            if in_metric_block:
                verdict = "blocked"
            elif in_describe_block:
                verdict = "describe_blocked"
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
        # the floor); "none" (empty block, but describe still uses
        # the floor). Computed from per-column intersections so the
        # operator sees the concrete impact of switching postures.
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
    in this module). WAL pragmas let the connection see rows committed
    by a concurrent ``schemabrain serve`` process.
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
    The sidecar still serves all 8 API routes; you can browse them
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
