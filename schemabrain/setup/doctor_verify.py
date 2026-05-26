"""`schemabrain doctor --verify` — mock-agent end-to-end smoke.

Simulates a complete MCP tool turn against the substrate WITHOUT
requiring an LLM key, an MCP host, or a running Claude Desktop
session. The four stages mirror what a real agent does on the first
query:

  1. ``list_entities`` — does the store carry any curated entities?
  2. ``describe_entity`` — can we resolve the first entity's full
     detail (fields, joins, PII tags)?
  3. ``find_relevant_entities`` — does semantic retrieval return
     hits for a generic query? Skipped when embeddings are missing
     (``--no-embed`` indexing path, fastembed unavailable).
  4. ``get_metric`` — does the metric execution pipeline run
     end-to-end against the source database? Skipped when no
     ``*_count`` metric exists, or when the operator didn't pass
     ``--source`` / ``--url-env`` (no executor available).

Each stage is timed with ``time.perf_counter``. Stages 1 + 2 are
required; failure in either sets the result's exit code to 2.
Stages 3 + 4 are best-effort; their skips don't fail the verify
but DO surface in the renderer so the operator knows what was
exercised vs what wasn't.

This module is deliberately decoupled from ``doctor_flow.py`` —
that module composes ``check_*`` helpers into a config-health
report; this one simulates an agent turn. The two surfaces share
nothing except the ``doctor`` CLI command and the ``Path`` /
``str`` plumbing.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

VerifyStageStatus = Literal["pass", "fail", "skipped"]


@dataclass(frozen=True)
class VerifyStage:
    """One stage of the mock-agent smoke.

    ``name`` is the stable identifier the renderer prints. ``status``
    is the three-state outcome. ``message`` is a one-line human
    summary (e.g. ``"3 entities visible"`` on pass,
    ``"store has no entities"`` on skip). ``duration_s`` is
    wall-clock from stage entry to outcome — even skipped stages
    record their (very short) duration so the renderer can show a
    consistent column.
    """

    name: str
    status: VerifyStageStatus
    message: str
    duration_s: float


@dataclass(frozen=True)
class VerifyResult:
    """Aggregate of ``verify_mock_agent``.

    ``stages`` is the tuple of per-stage outcomes in execution order.
    ``exit_code`` is 0 when no required stage failed, 2 when at
    least one did. ``total_duration_s`` is the sum of per-stage
    durations — useful for "did the substrate respond in <10s?" as
    promised by the audit recommendation.
    """

    stages: tuple[VerifyStage, ...]
    exit_code: int
    total_duration_s: float


def verify_mock_agent(
    *,
    store_path: Path,
    source_url: str | None,
) -> VerifyResult:
    """Run the four-stage mock-agent smoke against ``store_path``.

    Lazy-imports the heavy ``schemabrain/mcp/`` modules so importing
    this module is cheap when the operator runs ``doctor`` without
    ``--verify``. Each stage is wrapped in a try/except so a single
    stage failure surfaces in the result rather than crashing the
    whole verify.

    The verify intentionally does NOT mutate any store state — it
    only reads. Run it on a production store without risk; the
    ``get_metric`` stage executes a SELECT against ``source_url``
    when supplied but never writes.
    """
    from schemabrain.core.store import SQLiteStore

    stages: list[VerifyStage] = []
    overall_start = time.perf_counter()

    if not store_path.exists():
        # Refuse fast — the rest of the stages all need the store
        # open. Surface as a single failing stage so the renderer
        # produces a meaningful report instead of an empty one.
        return VerifyResult(
            stages=(
                VerifyStage(
                    name="store_present",
                    status="fail",
                    message=f"store not found at {store_path}",
                    duration_s=0.0,
                ),
            ),
            exit_code=2,
            total_duration_s=0.0,
        )

    with SQLiteStore(path=store_path) as store:
        source_id = _resolve_source_id(store)
        if source_id is None:
            return VerifyResult(
                stages=(
                    VerifyStage(
                        name="source_resolved",
                        status="fail",
                        message="store carries no source connection id (run `schemabrain init` first)",
                        duration_s=0.0,
                    ),
                ),
                exit_code=2,
                total_duration_s=time.perf_counter() - overall_start,
            )

        # Stage 1: list_entities (required).
        list_stage = _run_list_entities(store=store, source_id=source_id)
        stages.append(list_stage)
        if list_stage.status != "pass":
            return _finalise(stages, overall_start)

        # Stage 2: describe_entity on the first entity (required).
        # ``list_entities_impl`` returns at least one entity here
        # because the pass branch above proved it.
        from schemabrain.mcp.list_entities import list_entities_impl

        first_entity = list_entities_impl(store=store, source_connection_id=source_id)[0]
        describe_stage = _run_describe_entity(
            store=store, source_id=source_id, entity_name=first_entity.name
        )
        stages.append(describe_stage)
        if describe_stage.status != "pass":
            return _finalise(stages, overall_start)

        # Stage 3: find_relevant_entities (best-effort).
        stages.append(_run_find_relevant(store=store, source_id=source_id))

        # Stage 4: get_metric on the first *_count metric (best-effort).
        stages.append(_run_get_metric(store=store, source_id=source_id, source_url=source_url))

    return _finalise(stages, overall_start)


def _resolve_source_id(store: object) -> str | None:
    """Return the first source_connection_id known to the store, or None.

    The store can in principle carry multiple sources (one operator
    indexing two databases into the same store), but the v0.4 CLI
    surface only writes one — the wizard's stage-1 ``init`` flow
    stamps a single id derived from the source URL. Verify picks
    the first one it sees; that's the only one in practice.
    """
    conn = store._require_conn()  # type: ignore[attr-defined]  # internal helper, doctor-only access
    # `tables` is populated by the indexer; `entities` is populated by
    # the entity-suggest stage. Prefer `entities` since the verify
    # immediately calls `list_entities_impl` which requires at least
    # one entity row anyway — if `entities` is empty, the verify
    # would skip with a clearer message.
    row = conn.execute("SELECT DISTINCT source_connection_id FROM entities LIMIT 1").fetchone()
    if row is None:
        return None
    return str(row[0])


def _run_list_entities(*, store: object, source_id: str) -> VerifyStage:
    from schemabrain.mcp.list_entities import list_entities_impl

    started = time.perf_counter()
    try:
        entities = list_entities_impl(store=store, source_connection_id=source_id)  # type: ignore[arg-type]
    except Exception as exc:
        return VerifyStage(
            name="list_entities",
            status="fail",
            message=f"{type(exc).__name__}: {exc}",
            duration_s=time.perf_counter() - started,
        )
    if not entities:
        return VerifyStage(
            name="list_entities",
            status="fail",
            message="store has no curated entities (run `schemabrain entities suggest --apply`)",
            duration_s=time.perf_counter() - started,
        )
    return VerifyStage(
        name="list_entities",
        status="pass",
        message=f"{len(entities)} {'entity' if len(entities) == 1 else 'entities'} visible",
        duration_s=time.perf_counter() - started,
    )


def _run_describe_entity(*, store: object, source_id: str, entity_name: str) -> VerifyStage:
    from schemabrain.mcp.describe_entity import describe_entity_impl

    started = time.perf_counter()
    try:
        detail = describe_entity_impl(
            store=store,  # type: ignore[arg-type]
            source_connection_id=source_id,
            name=entity_name,
        )
    except Exception as exc:
        return VerifyStage(
            name="describe_entity",
            status="fail",
            message=f"{type(exc).__name__}: {exc}",
            duration_s=time.perf_counter() - started,
        )
    column_count = len(detail.columns)
    return VerifyStage(
        name="describe_entity",
        status="pass",
        message=f"resolved `{entity_name}` ({column_count} columns)",
        duration_s=time.perf_counter() - started,
    )


_VERIFY_QUERY = "what tables are available"
"""Generic agent query used by the find_relevant_entities stage.

Deliberately broad so the smoke succeeds on any schema (e-commerce,
DVD rental, finance). Specific queries like ``"customer orders"`` would
return zero hits on schemas without those tables and flake the verify
on non-canonical fixtures.
"""


def _run_find_relevant(*, store: object, source_id: str) -> VerifyStage:
    started = time.perf_counter()
    try:
        from schemabrain.enrichment.embeddings import fastembed_default
    except ImportError:
        return VerifyStage(
            name="find_relevant_entities",
            status="skipped",
            message="fastembed not importable (`--no-embed` path or Apple Silicon py3.12+ gap)",
            duration_s=time.perf_counter() - started,
        )

    from schemabrain.mcp.find_relevant_entities import find_relevant_entities_impl

    try:
        embedder = fastembed_default()
        hits = find_relevant_entities_impl(
            store=store,  # type: ignore[arg-type]
            source_connection_id=source_id,
            embedder=embedder,
            query=_VERIFY_QUERY,
            limit=3,
        )
    except Exception as exc:
        return VerifyStage(
            name="find_relevant_entities",
            status="fail",
            message=f"{type(exc).__name__}: {exc}",
            duration_s=time.perf_counter() - started,
        )
    if not hits:
        # No embeddings stored OR semantic search returned nothing.
        # Either is a degraded but not failing condition for verify —
        # the substrate works, the operator may want to re-index.
        return VerifyStage(
            name="find_relevant_entities",
            status="skipped",
            message="no embedding hits (re-index without `--no-embed` to enable semantic search)",
            duration_s=time.perf_counter() - started,
        )
    return VerifyStage(
        name="find_relevant_entities",
        status="pass",
        message=f"{len(hits)} hit(s) for `{_VERIFY_QUERY}`",
        duration_s=time.perf_counter() - started,
    )


def _run_get_metric(*, store: object, source_id: str, source_url: str | None) -> VerifyStage:
    started = time.perf_counter()
    if source_url is None:
        return VerifyStage(
            name="get_metric",
            status="skipped",
            message="no --source / --url-env passed; cannot build executor",
            duration_s=time.perf_counter() - started,
        )

    metrics = store.list_metrics(source_connection_id=source_id)  # type: ignore[attr-defined]
    count_metric = next((m for m in metrics if m.name.endswith("_count")), None)
    if count_metric is None:
        return VerifyStage(
            name="get_metric",
            status="skipped",
            message="no `*_count` metric in store (curate with `schemabrain metrics suggest --apply`)",
            duration_s=time.perf_counter() - started,
        )

    from sqlalchemy import create_engine

    from schemabrain.mcp.get_metric import get_metric_impl
    from schemabrain.mcp.metric_executor import EngineMetricExecutor

    try:
        engine = create_engine(
            source_url,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        executor = EngineMetricExecutor(engine)
        result = get_metric_impl(
            store=store,  # type: ignore[arg-type]
            executor=executor,
            source_connection_id=source_id,
            name=count_metric.name,
        )
    except Exception as exc:
        return VerifyStage(
            name="get_metric",
            status="fail",
            message=f"{type(exc).__name__}: {exc}",
            duration_s=time.perf_counter() - started,
        )
    finally:
        with contextlib.suppress(Exception):
            engine.dispose()  # type: ignore[possibly-undefined]

    rows = getattr(result, "rows", None) or getattr(result, "data", None) or []
    row_count = len(rows) if isinstance(rows, list) else 0
    return VerifyStage(
        name="get_metric",
        status="pass",
        message=f"executed `{count_metric.name}` ({row_count} row(s))",
        duration_s=time.perf_counter() - started,
    )


def _finalise(stages: list[VerifyStage], started: float) -> VerifyResult:
    """Build the final ``VerifyResult`` from accumulated stages."""
    exit_code = 0 if all(s.status != "fail" for s in stages) else 2
    return VerifyResult(
        stages=tuple(stages),
        exit_code=exit_code,
        total_duration_s=time.perf_counter() - started,
    )


def render_verify(result: VerifyResult, *, console: object) -> None:
    """Print the mock-agent verify result to ``console`` (stderr Rich).

    Layout::

      Mock-agent smoke (1.2s total)
      ✓ list_entities           3 entities visible           (0.0s)
      ✓ describe_entity         resolved `customer` (8 fields)  (0.0s)
      ⊘ find_relevant_entities  fastembed not importable     (0.0s)
      ✓ get_metric              executed `customer_count` (1 row)  (1.2s)

      Exit 0 — substrate green.

    Glyphs match the existing wizard renderer's convention (✓ / ✗ / ⊘)
    so operators reading both surfaces get a consistent signal.
    """
    glyphs = {"pass": "[green]✓[/]", "fail": "[red]✗[/]", "skipped": "[dim]⊘[/]"}
    width = max((len(s.name) for s in result.stages), default=0)
    console.print(  # type: ignore[attr-defined]
        f"Mock-agent smoke ([bold]{result.total_duration_s:.1f}s[/] total)"
    )
    for stage in result.stages:
        glyph = glyphs.get(stage.status, "?")
        console.print(  # type: ignore[attr-defined]
            f"{glyph} [bold]{stage.name:<{width}}[/]  {stage.message}  "
            f"[dim]({stage.duration_s:.1f}s)[/]"
        )
    console.print()  # type: ignore[attr-defined]
    if result.exit_code == 0:
        console.print("[green]Exit 0 — substrate green.[/]")  # type: ignore[attr-defined]
    else:
        console.print(  # type: ignore[attr-defined]
            "[red]Exit 2 — at least one required stage failed.[/]"
        )
