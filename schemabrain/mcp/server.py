"""FastMCP server wiring for Schema Brain.

`build_server(store, source_connection_id, embedder)` returns a
configured `FastMCP` instance with four tools registered:
`find_relevant_tables`, `describe_table`, `describe_column`, and
`suggest_joins`.

This module is the *boundary* between Schema Brain's pure-function tool
implementations (in `mcp/*.py`) and the MCP transport. Two boundary
concerns live here:

  1. Envelope construction — every tool returns a `ToolResponse[T]` per
     Charter v1.0. No `*_impl` exception ever propagates through MCP;
     each is caught and mapped to a `status="error"` envelope with the
     right `kind` + `recovery` hint.
  2. Tool metadata — per-tool `ToolAnnotations` (canonical MCP hints
     for spec-compliant clients) and "Use this when…" descriptions
     (Principle 2). Both are CI-lintable.

`run_stdio()` is a convenience that builds the server and runs it on
stdio (the transport Claude Desktop and most local-MCP clients use).
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.mcp.describe_table import describe_table_impl
from schemabrain.mcp.envelope import (
    Provenance,
    Recovery,
    ToolError,
    ToolResponse,
)
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl
from schemabrain.mcp.shapes import (
    ColumnDetail,
    ColumnNotFoundError,
    SuggestJoinsResult,
    TableDescription,
    TableHit,
    TableNotFoundError,
)
from schemabrain.mcp.suggest_joins import suggest_joins_impl

_DEFAULT_LIMIT = 10
_DEFAULT_MAX_HOPS = 4
_SERVER_NAME = "schemabrain"
_SERVER_INSTRUCTIONS = (
    "Schema Brain — semantic understanding of an indexed database. "
    "Use `find_relevant_tables` to discover which tables are relevant to "
    "a question, `describe_table` to get the full structural and semantic "
    "shape of one table, `describe_column` to drill into a single column "
    "(including which other tables join in to it), and `suggest_joins` "
    "to get ranked FK-graph join paths between two or more tables. Every "
    "tool returns a `ToolResponse` envelope (status / data / error / "
    "confidence / follow_up_hints) per the agent-UX charter v1.0."
)

_logger = logging.getLogger(__name__)

# Confidence buckets per Charter Principle 4. The thresholds are a
# calibration knob — adjusted as agent task-success data accumulates.
_CONFIDENCE_HIGH_FLOOR = 0.8
_CONFIDENCE_MEDIUM_FLOOR = 0.5

# Canonical MCP hints shared by every v1.0 tool. All four are read-only,
# idempotent, and touch an open world (the indexed source's data may
# change between calls). Verified in CI against a per-tool manifest.
_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _confidence_from_score(score: float) -> str:
    """Bucket a cosine score into HIGH/MEDIUM/LOW per Charter v1.0."""
    if score >= _CONFIDENCE_HIGH_FLOOR:
        return "HIGH"
    if score >= _CONFIDENCE_MEDIUM_FLOOR:
        return "MEDIUM"
    return "LOW"


def _safe_table_part(qualified_name: str) -> str | None:
    """Pull the table name out of `schema.table` for a recovery hint.

    Returns `None` if the input isn't shaped like a two-part qualified
    name. Used to populate `suggested_args.query` when an `unknown_name`
    error points the agent back to `find_relevant_tables`.
    """
    parts = qualified_name.split(".")
    if len(parts) == 2 and all(parts):
        return parts[1]
    if len(parts) == 3 and all(parts):
        return parts[1]
    return None


def build_server(
    *,
    store: Store,
    source_connection_id: str,
    embedder: Embedder,
) -> FastMCP:
    """Build (but do not run) a configured `FastMCP` app.

    The store, source ID, and embedder are captured by closure on the
    registered tool callables. Returned app is ready to `.run("stdio")`
    or to be exercised in-process via `app.call_tool(...)`.
    """
    app = FastMCP(_SERVER_NAME, instructions=_SERVER_INSTRUCTIONS)

    @app.tool(
        description=(
            "Use this when the user describes tables semantically (e.g. "
            "'the table with customer orders', 'where we store payments'). "
            "Returns ranked hits with cosine scores plus the matched "
            "column and its LLM description so you see WHY each table "
            "surfaced. Use `describe_table` instead when the user names a "
            "specific table by qualified name. Common compositions: chain "
            "to `describe_table` for semantic-to-structural queries; chain "
            "to `suggest_joins` to discover then wire multi-table queries."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    def find_relevant_tables(
        query: str, limit: int = _DEFAULT_LIMIT
    ) -> ToolResponse[list[TableHit]]:
        try:
            hits = find_relevant_tables_impl(
                store=store,
                source_connection_id=source_connection_id,
                embedder=embedder,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        if not hits:
            # Tool ran cleanly; no matches in the indexed store. Per
            # Charter Principle 1, this is `empty`, not `success` with
            # an empty list.
            return ToolResponse[list[TableHit]](
                status="empty",
                data=[],
                confidence=None,
                follow_up_hints=None,
            )
        top_score = hits[0].score
        return ToolResponse[list[TableHit]](
            status="success",
            data=hits,
            confidence=_confidence_from_score(top_score),
            follow_up_hints=["describe_table"],
        )

    @app.tool(
        description=(
            "Use this when the user names a specific table by qualified "
            "name (e.g. 'show me public.orders'). Returns columns with "
            "types, nullability, primary-key flags, LLM descriptions, and "
            "outgoing foreign keys. Use `find_relevant_tables` instead "
            "when the user describes the table semantically. Common "
            "compositions: chain to `describe_column` to drill into one "
            "column's join graph; chain to `suggest_joins` to find paths "
            "from this table to others."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    def describe_table(qualified_name: str) -> ToolResponse[TableDescription]:
        try:
            table = describe_table_impl(
                store=store,
                source_connection_id=source_connection_id,
                qualified_name=qualified_name,
            )
        except ValueError as exc:
            return _malformed_name_response(
                exc,
                suggested_tool="find_relevant_tables",
            )
        except TableNotFoundError as exc:
            return _unknown_name_response(
                exc,
                suggested_tool="find_relevant_tables",
                suggested_args=_maybe_query_arg(qualified_name),
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        return ToolResponse[TableDescription](
            status="success",
            data=table,
            confidence="HIGH",
            follow_up_hints=["describe_column", "suggest_joins"],
        )

    @app.tool(
        description=(
            "Use this when you need to drill into one column by its "
            "three-part qualified name (e.g. `public.orders.user_id`). "
            "Returns data type, nullability, default, LLM description, "
            "and BOTH join directions — outgoing FKs (this column joins "
            "out) and incoming FKs (which tables reference this column). "
            "Use `describe_table` instead when you want the whole table "
            "at once. Common composition: chain `describe_table` to "
            "`describe_column` to map a column's full role across schema."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    def describe_column(qualified_name: str) -> ToolResponse[ColumnDetail]:
        try:
            column = describe_column_impl(
                store=store,
                source_connection_id=source_connection_id,
                qualified_name=qualified_name,
            )
        except ValueError as exc:
            return _malformed_name_response(
                exc,
                suggested_tool="find_relevant_tables",
            )
        except ColumnNotFoundError as exc:
            # Column missing but table exists → recovery points to the
            # parent table so the agent can see the real column list.
            parent_qn = _parent_table_qualified_name(qualified_name)
            recovery_args: dict[str, object] | None = (
                {"qualified_name": parent_qn} if parent_qn is not None else None
            )
            return ToolResponse[ColumnDetail](
                status="error",
                error=ToolError(
                    kind="unknown_name",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="describe_table",
                        suggested_args=recovery_args,
                    ),
                ),
            )
        except TableNotFoundError as exc:
            # Parent table missing too → recovery routes to discovery.
            return _unknown_name_response(
                exc,
                suggested_tool="find_relevant_tables",
                suggested_args=_maybe_query_arg(qualified_name),
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        return ToolResponse[ColumnDetail](
            status="success",
            data=column,
            confidence="HIGH",
            follow_up_hints=["describe_table"],
        )

    @app.tool(
        description=(
            "Use this when you already know two or more tables and need "
            "the join paths between them. Pass qualified names "
            "(`schema.table`) and get one shortest FK path per pair, "
            "with columns on each side ready for a SQL JOIN. Multi-hop "
            "paths via intermediates are returned; pairs with no path "
            "within `max_hops` (default 4) land in `unreachable_pairs`. "
            "Use `find_relevant_tables` instead when you don't yet know "
            "the table names. Common composition: chain "
            "`find_relevant_tables` to `suggest_joins`."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    def suggest_joins(
        tables: list[str], max_hops: int = _DEFAULT_MAX_HOPS
    ) -> ToolResponse[SuggestJoinsResult]:
        try:
            result = suggest_joins_impl(
                store=store,
                source_connection_id=source_connection_id,
                tables=tables,
                max_hops=max_hops,
            )
        except ValueError as exc:
            return _malformed_name_response(
                exc,
                suggested_tool="find_relevant_tables",
            )
        except TableNotFoundError as exc:
            return _unknown_name_response(
                exc,
                suggested_tool="find_relevant_tables",
                suggested_args=None,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        return ToolResponse[SuggestJoinsResult](
            status="success",
            data=result,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            follow_up_hints=["describe_table"],
        )

    return app


def _malformed_name_response(exc: Exception, *, suggested_tool: str) -> ToolResponse:
    """Build a `malformed_name` error envelope.

    Used whenever an `*_impl` raises `ValueError` — either because a
    qualified name had the wrong shape or because another caller-shape
    constraint failed (e.g. `suggest_joins` with `max_hops <= 0`). All
    of these are retryable with corrected args, so recovery points the
    agent at a discovery tool to start over.
    """
    return ToolResponse(
        status="error",
        error=ToolError(
            kind="malformed_name",
            message=str(exc),
            recovery=Recovery(suggested_tool=suggested_tool),
        ),
    )


def _unknown_name_response(
    exc: Exception,
    *,
    suggested_tool: str,
    suggested_args: dict[str, object] | None,
) -> ToolResponse:
    """Build an `unknown_name` error envelope. Used for
    `TableNotFoundError` and the unknown-parent-table variant of
    `describe_column`.
    """
    return ToolResponse(
        status="error",
        error=ToolError(
            kind="unknown_name",
            message=str(exc),
            recovery=Recovery(
                suggested_tool=suggested_tool,
                suggested_args=suggested_args,
            ),
        ),
    )


def _wrap_internal_error(exc: Exception) -> ToolResponse:
    """Catch-all for unexpected exceptions.

    Charter v1.0 marks `internal_error` as "A bug; the agent should not
    retry. Logged for repair." We honor both halves: the full traceback
    goes to the server-side logger where operators can act on it, and
    the client receives a generic message — exposing `str(exc)` to the
    MCP client would leak server paths or state for some exceptions.
    """
    _logger.error(
        "internal_error boundary catch: unexpected exception in MCP tool",
        exc_info=exc,
    )
    return ToolResponse(
        status="error",
        error=ToolError(
            kind="internal_error",
            message=("An unexpected internal error occurred. Check server logs for details."),
            recovery=Recovery(),
        ),
    )


def _maybe_query_arg(qualified_name: str) -> dict[str, object] | None:
    """Build a `{"query": <table-name>}` arg dict for the recovery hint
    on `unknown_name` errors, or None if the input is too malformed to
    extract a useful query.
    """
    candidate = _safe_table_part(qualified_name)
    if candidate is None:
        return None
    return {"query": candidate}


def _parent_table_qualified_name(column_qualified_name: str) -> str | None:
    """`public.users.email` → `public.users`. None if input is not
    three-part shaped.
    """
    parts = column_qualified_name.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    return f"{parts[0]}.{parts[1]}"


def run_stdio(
    *,
    store: Store,
    source_connection_id: str,
    embedder: Embedder,
) -> None:
    """Build the server and run it forever on stdio.

    Blocks until the client disconnects. Used by the `schemabrain serve`
    CLI subcommand.

    Defensively configures stderr-only logging if no caller has done so
    already — stdout is the JSON-RPC wire here, and a stray log byte
    would corrupt the MCP frame. Skipped entirely when the CLI (or any
    other caller) already attached our named handler, so the caller's
    chosen verbosity is respected.
    """
    import logging as _logging

    from schemabrain.logging_config import _HANDLER_NAME, configure_logging

    pkg_logger = _logging.getLogger("schemabrain")
    already_configured = any(getattr(h, "name", None) == _HANDLER_NAME for h in pkg_logger.handlers)
    if not already_configured:
        configure_logging()
    app = build_server(store=store, source_connection_id=source_connection_id, embedder=embedder)
    app.run(transport="stdio")
