"""FastMCP server wiring for Schema Brain.

`build_server(store, source_connection_id, embedder)` returns a
configured `FastMCP` instance with ten tools registered. Five
physical-schema tools: `find_relevant_tables`, `describe_table`,
`describe_column`, `suggest_joins`, `get_example_queries`. Five
semantic-layer tools: `find_relevant_entities`, `list_entities`,
`describe_entity`, `resolve_join`, `get_metric`.

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
import uuid
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Any, TypeAlias

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError
from mcp.types import ContentBlock, ToolAnnotations
from pydantic import Field

from schemabrain.audit.writer import AuditWriter
from schemabrain.core.store_protocol import Store
from schemabrain.enrichment.embeddings import Embedder
from schemabrain.mcp._helpers import _MAX_IDENT_LEN
from schemabrain.mcp.describe_column import describe_column_impl
from schemabrain.mcp.describe_entity import describe_entity_impl
from schemabrain.mcp.describe_table import describe_table_impl
from schemabrain.mcp.envelope import (
    DegradationReason,
    Provenance,
    Recovery,
    ToolError,
    ToolResponse,
)
from schemabrain.mcp.find_relevant_entities import find_relevant_entities_impl
from schemabrain.mcp.find_relevant_tables import find_relevant_tables_impl
from schemabrain.mcp.get_example_queries import get_example_queries_impl
from schemabrain.mcp.get_metric import get_metric_impl
from schemabrain.mcp.list_entities import list_entities_impl
from schemabrain.mcp.list_joins import list_joins_impl
from schemabrain.mcp.list_metrics import list_metrics_impl
from schemabrain.mcp.metric_executor import MetricExecutor
from schemabrain.mcp.resolve_join import resolve_join_impl
from schemabrain.mcp.shapes import (
    AmbiguousJoinError,
    CanonicalJoinInfo,
    ColumnDetail,
    ColumnNotFoundError,
    EntityDetail,
    EntityHit,
    EntityNotFoundError,
    EntitySummary,
    ExampleQueriesResult,
    JoinNameMismatchError,
    JoinSummary,
    MetricFilterArg,
    MetricOrderByArg,
    MetricResult,
    MetricSummary,
    NoCanonicalJoinError,
    SuggestJoinsResult,
    TableDescription,
    TableHit,
    TableNotFoundError,
    UnknownJoinNameError,
)
from schemabrain.mcp.suggest_joins import suggest_joins_impl
from schemabrain.observability import (
    EventBus,
    EventRedactor,
    NullEventBus,
    instrument,
)

# Re-use the same private OSError-vs-Exception-split helpers that
# `instrument()` uses for successful tool calls. We import the
# underscored helpers (rather than re-implementing) so the rejection
# path gets the same dedup-once-for-OSError + log-every-time-for-
# programming-bug split — without forcing those helpers into the
# public observability surface area. If their signatures change, the
# rejection path follows. (An earlier hand-rolled try/except here
# flattened the OSError-vs-programming-bug distinction; delegating
# preserves it.)
from schemabrain.observability.instrument import (
    _safe_audit_write,
    _safe_emit,
)
from schemabrain.pii import PIICategory
from schemabrain.semantic.compiler import (
    AmbiguousJoinError as CompilerAmbiguousJoinError,
)
from schemabrain.semantic.compiler import (
    AmbiguousPathError as CompilerAmbiguousPathError,
)
from schemabrain.semantic.compiler import (
    InvalidTimeGrainError,
    MalformedColumnError,
    PiiBlockedError,
    UnknownColumnError,
    UnknownMetricError,
    UnknownOrderByColumnError,
    UnknownViaJoinError,
    UnreachableEntityError,
)

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

_DEFAULT_LIMIT = 10
_DEFAULT_MAX_HOPS = 6
_SERVER_NAME = "schemabrain"
_SERVER_INSTRUCTIONS = (
    "Schema Brain — semantic understanding of an indexed database. "
    "Physical-schema tools: `find_relevant_tables` to discover tables, "
    "`describe_table` for one table's full shape, `describe_column` to "
    "drill into a single column (with join graph), `suggest_joins` for "
    "FK-graph paths between tables, `get_example_queries` for real SQL "
    "patterns from query logs. Semantic-layer tools: "
    "`find_relevant_entities` to discover entities by semantic query, "
    "`list_entities` to survey defined entities, `list_metrics` to "
    "survey defined metrics — start here for any aggregation question "
    "('how many', 'total', 'average', 'most', 'top') so you can pick "
    "the right metric before computing, `list_joins` to survey defined "
    "canonical joins, `describe_entity` for one entity's full column "
    "shape with PII sensitivity, `resolve_join` to return the "
    "canonical SQL JOIN between two entities (with ambiguity refusal "
    "when multiple canonical joins exist), `get_metric` to compute a "
    "validated aggregation against an entity — automatically chains "
    "multi-hop canonical joins (e.g. `order_item → order → user`) so "
    "you can `group_by` an entity that's reachable via any chain of "
    "joins, not just one hop. Every tool returns a `ToolResponse` "
    "envelope (status / data / error / confidence / follow_up_hints) "
    "per the agent-UX charter v1.1."
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

    Length-bounded defensively: even though every current caller runs
    through `_parse_qualified_name` first (so parts are at most
    `_MAX_IDENT_LEN`), this function should be safe at any callsite
    in case a future code path skips parse validation.
    """
    parts = qualified_name.split(".")
    if len(parts) in (2, 3) and all(parts):
        candidate = parts[1]
        if len(candidate) > _MAX_IDENT_LEN:
            return None
        return candidate
    return None


def _build_rejection_response(err_msg: str) -> ToolResponse[None]:
    """Construct a synthetic `ToolResponse` for an extra-kwargs
    rejection. Status `error` + `kind: invalid_argument` lets the
    audit-row builder and event extractor treat the rejection
    identically to any other in-band tool error.
    """
    return ToolResponse[None](
        status="error",
        data=None,
        error=ToolError(
            kind="invalid_argument",
            message=err_msg,
            recovery=Recovery(),
        ),
    )


# Hook fired on every unknown-kwargs rejection. Tuple is
# `(tool_name, arguments_sent, error_message)` — named via TypeAlias
# (vs inline `Callable[...]`) so callers see the parameter intent
# without re-reading the subclass docstring.
RejectionHook: TypeAlias = Callable[[str, dict[str, Any], str], None]


class _StrictArgsFastMCP(FastMCP):
    """FastMCP subclass that rejects unknown kwargs at the dispatch
    seam, and forwards each rejection to a caller-supplied hook so it
    can land in the audit table + event bus alongside successful calls.

    Why: FastMCP's auto-generated Pydantic arg models default to
    `extra="ignore"`, so a typo'd kwarg (e.g. `grain` for `time_grain`
    on `get_metric`) silently passes through and the tool runs with
    the typo dropped — producing a structurally valid response that
    answers the wrong question. The Charter's "loud, not silent"
    principle requires rejecting these.

    The override runs BEFORE FastMCP's own validation so the rejection
    event carries the exact arg dict the agent sent, not what survived
    validation. Raises `FastMCPToolError` after the hook fires; FastMCP
    surfaces this to the client as `isError: true` with the message
    naming the offending keys, so the agent sees a real "fix your
    args" signal instead of getting a silently-wrong answer.

    The `on_rejected` hook is wired in `build_server` to call the
    same audit-writer and event-bus shared by `instrument()`.
    """

    def __init__(
        self,
        *args: Any,
        on_rejected: RejectionHook | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_rejected = on_rejected

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        tool = self._tool_manager.get_tool(name)
        # If the tool isn't registered we let the parent raise its
        # standard "Unknown tool" — no extra-kwarg check applies.
        # `tool.fn_metadata` is non-optional in the upstream FastMCP
        # type (`FuncMetadata`, always populated by `Tool.from_function`)
        # so we read `arg_model.model_fields` directly without a guard.
        if tool is not None and isinstance(arguments, dict):
            declared = set(tool.fn_metadata.arg_model.model_fields.keys())
            extras = sorted(set(arguments.keys()) - declared)
            if extras:
                err_msg = f"unknown arguments for {name}: {extras}. declared: {sorted(declared)}"
                if self._on_rejected is not None:
                    self._on_rejected(name, arguments, err_msg)
                raise FastMCPToolError(err_msg)
        return await super().call_tool(name, arguments)


def build_server(
    *,
    store: Store,
    source_connection_id: str,
    embedder: Embedder,
    metric_executor: MetricExecutor | None = None,
    event_bus: EventBus | None = None,
    server_session_id: str | None = None,
    audit_writer: AuditWriter | None = None,
    pii_block: frozenset[PIICategory] = frozenset(),
    tracer: Tracer | None = None,
) -> FastMCP:
    """Build (but do not run) a configured `FastMCP` app.

    The store, source ID, and embedder are captured by closure on the
    registered tool callables. Returned app is ready to `.run("stdio")`
    or to be exercised in-process via `app.call_tool(...)`.

    `metric_executor` is required when `get_metric` is exercised — it
    carries the SQLAlchemy engine that runs compiled SQL against the
    source database. Tests inject stubs; the `run_stdio` entrypoint
    builds an `EngineMetricExecutor` keyed on the same URL passed to
    `index`. Passing `None` registers `get_metric` but every call
    raises `internal_error` with "executor not configured" — a
    structural mistake on the operator side.

    `event_bus` is the observability sink — each tool call emits one
    Event on success and on failure. Defaults to `NullEventBus`
    (no-op) so tests and callers that don't care don't pay the cost.
    `server_session_id` is a UUID identifying this server process
    so a tail reader can group events across a single `serve` run;
    a fresh UUID4 is generated when not supplied.

    `audit_writer` is the optional `mcp_audit` table writer. When set,
    every tool call writes one row before the bus emit; the row's
    fingerprint hex is injected into `MetricResult.fingerprint` so
    agents see a real hash rather than the v0 stub. Defaults to None
    (no audit writes) so test contexts that don't care don't pay the
    SQLite syscall cost.

    `tracer` is the optional OpenTelemetry tracer. When non-None, each
    tool call is wrapped in a span tagged with `gen_ai.*` attributes
    that flow over OTLP/HTTP to whatever endpoint
    `OTEL_EXPORTER_OTLP_ENDPOINT` points at (Langfuse, Phoenix,
    OpenLIT, otel-tui, etc.). Constructed via `init_tracer_from_env()`
    in `_cmd_serve`; defaults to None so test contexts and the
    `schemabrain[otel]`-not-installed case pay zero overhead.
    """
    _bus = event_bus if event_bus is not None else NullEventBus()
    _session_id = server_session_id or str(uuid.uuid4())
    _redactor = EventRedactor()

    def _on_rejected_call(name: str, arguments: dict[str, Any], err_msg: str) -> None:
        """Record an unknown-kwargs rejection in BOTH the audit table
        and the event bus, mirroring the shape `instrument()` writes
        for successful calls. Without this, the `audit list` view and
        events.jsonl would silently omit rejected calls — exactly the
        visibility gap S4 calls out.

        Delegates to `_safe_audit_write` and `_safe_emit` so the
        rejection path gets the same OSError-vs-Exception split as
        instrumented successful calls (OSError dedup'd once per
        (sink, tool); programming bugs printed to stderr every time
        so a fresh contributor sees the regression immediately).

        `args=()` + `kwargs=arguments` matches FastMCP's normal
        kwargs-only dispatch shape, so the redactor sees the same
        envelope it sees on successful calls. `duration_ms=0.0`
        because the rejection short-circuits BEFORE the tool body
        runs — there's no meaningful timing to record.
        """
        synthetic = _build_rejection_response(err_msg)
        if audit_writer is not None:
            _safe_audit_write(
                writer=audit_writer,
                tool_name=name,
                source_connection_id=source_connection_id,
                response=synthetic,
            )
        _safe_emit(
            bus=_bus,
            redactor=_redactor,
            tool_name=name,
            server_session_id=_session_id,
            args=(),
            kwargs=arguments,
            response=synthetic,
            duration_ms=0.0,
        )

    app = _StrictArgsFastMCP(
        _SERVER_NAME,
        instructions=_SERVER_INSTRUCTIONS,
        on_rejected=_on_rejected_call,
    )

    def _trace(name: str):
        return instrument(
            tool_name=name,
            bus=_bus,
            redactor=_redactor,
            server_session_id=_session_id,
            audit_writer=audit_writer,
            source_connection_id=source_connection_id,
            tracer=tracer,
        )

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
    @_trace("find_relevant_tables")
    def find_relevant_tables(
        query: Annotated[
            str,
            Field(
                description=(
                    "Natural-language description of the table or data the "
                    "user is asking about (e.g. 'customer orders', 'where "
                    "we store payments'). Embedded with the same model used "
                    "to index column descriptions, then ranked by cosine "
                    "similarity against per-column descriptions."
                ),
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of ranked hits to return. Default 10. "
                    "Use a small value (3-5) for narrow exploratory queries; "
                    "use 10-20 when surveying an unfamiliar schema."
                ),
            ),
        ] = _DEFAULT_LIMIT,
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
            # an empty list. Hand back `describe_table` as the agent's
            # fallback — it's the only related tool that doesn't
            # depend on semantic embeddings. If the agent knows a name
            # (from `list_entities` or earlier context) it can drill
            # directly; if not, surfacing a non-null hint still beats
            # a silent dead-end and signals that more discovery is
            # possible. The most common cause of empty here is that
            # `index` was run without `--enrich`, so there are no
            # embedded descriptions to search — but distinguishing
            # that case requires a second store probe that isn't worth
            # the extra round-trip for this minor UX gain.
            return ToolResponse[list[TableHit]](
                status="empty",
                data=[],
                confidence=None,
                follow_up_hints=("describe_table",),
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
            "Use this when the user describes a business object (e.g. "
            "'our customers', 'revenue data', 'product catalog'). "
            "Returns ranked entities — domain-named bindings to physical "
            "tables — so the agent stays in business terms. Use "
            "`find_relevant_tables` instead when no entities are curated. "
            "Common compositions: "
            "chain to `describe_entity` for one entity's full shape; "
            "chain to `resolve_join` to wire two entities together; "
            "chain to `get_metric` to compute a validated aggregation."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("find_relevant_entities")
    def find_relevant_entities(
        query: Annotated[
            str,
            Field(
                description=(
                    "Natural-language description of the business object "
                    "the user is asking about (e.g. 'customers', 'revenue', "
                    "'product catalog'). Embedded with the same model used "
                    "to index column descriptions, then ranked by cosine "
                    "similarity against the columns of each entity's bound "
                    "table. Per-entity score is the MAX across columns."
                ),
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of ranked entity hits to return. "
                    "Default 10. Use a small value (3-5) for narrow "
                    "exploratory queries; use 10-20 when surveying an "
                    "unfamiliar semantic layer."
                ),
            ),
        ] = _DEFAULT_LIMIT,
    ) -> ToolResponse[list[EntityHit]]:
        try:
            hits = find_relevant_entities_impl(
                store=store,
                source_connection_id=source_connection_id,
                embedder=embedder,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        if not hits:
            # Two sub-cases under `empty`, distinguished by hint routing
            # so the agent gets a different signal for each:
            #   (a) NO entities curated yet → `list_entities` would
            #       also return empty, so omit it from hints and route
            #       the agent only to `find_relevant_tables` (physical
            #       discovery as the next move).
            #   (b) Entities curated but none semantically match → both
            #       hints are useful: `list_entities` so the agent can
            #       confirm the entity surface, `find_relevant_tables`
            #       as a physical-side fallback.
            # The probe is a second call to `list_entities` rather than
            # threading state out of the impl — keeps the impl's return
            # shape narrow (`list[EntityHit]`) and the differentiation
            # logic local to the envelope-shaping boundary.
            entities = store.list_entities(source_connection_id=source_connection_id)
            hints = (
                ["find_relevant_tables"]
                if not entities
                else [
                    "list_entities",
                    "find_relevant_tables",
                ]
            )
            return ToolResponse[list[EntityHit]](
                status="empty",
                data=[],
                confidence=None,
                follow_up_hints=hints,
            )
        top_score = hits[0].score
        return ToolResponse[list[EntityHit]](
            status="success",
            data=hits,
            confidence=_confidence_from_score(top_score),
            follow_up_hints=["describe_entity"],
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
    @_trace("describe_table")
    def describe_table(
        qualified_name: Annotated[
            str,
            Field(
                description=(
                    "Postgres `schema.table` qualified name "
                    "(e.g. `public.orders`). Call `find_relevant_tables` "
                    "first if you don't know the schema."
                ),
            ),
        ],
    ) -> ToolResponse[TableDescription]:
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
    @_trace("describe_column")
    def describe_column(
        qualified_name: Annotated[
            str,
            Field(
                description=(
                    "Postgres `schema.table.column` qualified name "
                    "(e.g. `public.orders.user_id`). Three dot-separated "
                    "parts. Call `describe_table` first if you only know "
                    "the table and need to discover its columns."
                ),
            ),
        ],
    ) -> ToolResponse[ColumnDetail]:
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
            "Use this when you need real example SQL for an indexed "
            "table to learn how it's actually used. Each item carries "
            "the SQL text, observation count, source, and PII "
            "categories touched. Returns `status: empty` when the "
            "table has no recorded examples yet (query log mining "
            "ships next). Use `describe_table` instead when you want "
            "the table's structural shape rather than usage patterns. "
            "Common composition: chain `find_relevant_tables` to "
            "`get_example_queries`."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("get_example_queries")
    def get_example_queries(
        qualified_name: Annotated[
            str,
            Field(
                description=(
                    "Postgres `schema.table` qualified name "
                    "(e.g. `public.orders`). Returns SQL agents (or humans) "
                    "have actually run against this table, sourced from "
                    "`pg_stat_statements`. Run `schemabrain mine-queries` "
                    "first to populate the cache; until then this tool "
                    "returns `status: empty`."
                ),
            ),
        ],
    ) -> ToolResponse[ExampleQueriesResult]:
        try:
            result = get_example_queries_impl(
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
        if not result.queries:
            # Charter Principle 1: empty examples is `empty`, not
            # `success` with an empty list. Recovery hint points the
            # agent at `describe_table` so it has an actionable next
            # step when usage data isn't yet available.
            return ToolResponse[ExampleQueriesResult](
                status="empty",
                data=result,
                confidence=None,
                follow_up_hints=["describe_table"],
            )
        return ToolResponse[ExampleQueriesResult](
            status="success",
            data=result,
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
            "within `max_hops` (default 6) land in `unreachable_pairs`. "
            "Use `find_relevant_tables` instead when you don't yet know "
            "the table names. Common composition: chain "
            "`find_relevant_tables` to `suggest_joins`."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("suggest_joins")
    def suggest_joins(
        tables: Annotated[
            list[str],
            Field(
                description=(
                    "List of `schema.table` qualified names (minimum 2) "
                    "to find join paths between. The tool returns one "
                    "shortest FK path per unordered pair, plus an "
                    "`unreachable_pairs` list for pairs with no path "
                    "within `max_hops`."
                ),
            ),
        ],
        max_hops: Annotated[
            int,
            Field(
                description=(
                    "Maximum number of FK-graph hops to traverse when "
                    "searching for join paths. Default 6 — covers M:N "
                    "junction-table chains common in normalised OLTP "
                    "schemas. Increase only for unusually deep schemas; "
                    "higher values make the search non-trivially slower."
                ),
            ),
        ] = _DEFAULT_MAX_HOPS,
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

    @app.tool(
        description=(
            "Use this when the user asks what semantic entities are "
            "defined (e.g. 'what entities do we have?', 'show me the "
            "entity list'). Returns every confirmed entity with its "
            "bound table, identity column, and provenance. Use "
            "`describe_entity` instead when you already know the "
            "entity name and want its full column shape. Common "
            "compositions: chain to `describe_entity` to drill in; "
            "chain to `find_relevant_tables` to discover physical "
            "tables that should become entities."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("list_entities")
    def list_entities() -> ToolResponse[list[EntitySummary]]:
        try:
            summaries = list_entities_impl(
                store=store,
                source_connection_id=source_connection_id,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        if not summaries:
            # Charter Principle 1: an indexed store with no defined
            # entities yet is `empty`, not `success` with `[]`. The
            # follow-up hint points the agent at discovery so it has
            # an actionable next step when the semantic layer is bare.
            return ToolResponse[list[EntitySummary]](
                status="empty",
                data=[],
                confidence=None,
                follow_up_hints=["find_relevant_tables"],
            )
        return ToolResponse[list[EntitySummary]](
            status="success",
            data=summaries,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            follow_up_hints=["describe_entity"],
        )

    @app.tool(
        description=(
            "Use this when the user asks any ranking, top-N, "
            "most/highest/lowest, or aggregation question (e.g. 'who "
            "bought the most', 'top 5 by revenue', 'rank customers', "
            "'find users with the highest X', 'what's the total / "
            "average / count') — returns every declared metric with its "
            "anchor entity, aggregation, and time-bucketing so you can "
            "pick the right metric before calling `get_metric`. Use "
            "`get_metric` instead when you already have the metric "
            "name. Chain to `describe_entity` for full anchor shape."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("list_metrics")
    def list_metrics() -> ToolResponse[list[MetricSummary]]:
        try:
            summaries = list_metrics_impl(
                store=store,
                source_connection_id=source_connection_id,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        if not summaries:
            # Mirror `list_entities`: an indexed store with entities but
            # no metrics is `empty`, not `success` with `[]`. The
            # follow-up hint points the agent at the right discovery
            # next step — `list_entities` so it can ask the operator
            # to anchor a metric on one.
            return ToolResponse[list[MetricSummary]](
                status="empty",
                data=[],
                confidence=None,
                follow_up_hints=["list_entities"],
            )
        return ToolResponse[list[MetricSummary]](
            status="success",
            data=summaries,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            # `describe_entity` is the natural drill — `MetricSummary`
            # exposes `measure_column` as an opaque identifier; the
            # agent needs the anchor entity's full column shape to
            # verify what the column contains before calling
            # `get_metric` with a `group_by` that references it.
            follow_up_hints=["get_metric", "describe_entity"],
        )

    @app.tool(
        description=(
            "Use this when the user asks what canonical joins are "
            "defined (e.g. 'how are these entities connected?'). "
            "Returns each confirmed join with the entity pair it "
            "connects and provenance. Use `resolve_join` instead "
            "when you have a known pair and want the SQL skeleton "
            "(pass `name=` for 2+ joins on the pair). Use "
            "`suggest_joins` instead when only physical-table names "
            "are available."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("list_joins")
    def list_joins() -> ToolResponse[list[JoinSummary]]:
        try:
            summaries = list_joins_impl(
                store=store,
                source_connection_id=source_connection_id,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        if not summaries:
            # Mirror `list_entities` + `list_metrics`: indexed store
            # with no canonical joins yet is `empty`, not `success`
            # with `[]`. The follow-up hint points at
            # `find_relevant_tables` (start from physical schema)
            # rather than `suggest_joins` — when stage 5 of the
            # wizard ran and produced zero candidates, redirecting
            # back to `suggest_joins` would be a loop. The agent
            # should reason about join paths from the physical
            # schema instead.
            return ToolResponse[list[JoinSummary]](
                status="empty",
                data=[],
                confidence=None,
                follow_up_hints=["find_relevant_tables"],
            )
        return ToolResponse[list[JoinSummary]](
            status="success",
            data=summaries,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            follow_up_hints=["resolve_join"],
        )

    @app.tool(
        description=(
            "Use this when the user names a specific entity (e.g. "
            "'show me the customer entity', 'what's in the order "
            "entity'). Returns the entity's bound table, identity "
            "column, description, and full column list with PII "
            "sensitivity. Use `list_entities` instead when you "
            "don't yet know what entities exist. Common "
            "compositions: chain to `describe_table` to see the "
            "physical structure under the entity; chain to "
            "`describe_column` for one column's join graph."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("describe_entity")
    def describe_entity(
        name: Annotated[
            str,
            Field(
                description=(
                    "Entity name (a single identifier — no dots, no "
                    "schema qualifier; e.g. `customer`, not "
                    "`public.customer`). Call `list_entities` first if "
                    "you don't know the entity names."
                ),
            ),
        ],
    ) -> ToolResponse[EntityDetail]:
        try:
            detail = describe_entity_impl(
                store=store,
                source_connection_id=source_connection_id,
                name=name,
            )
        except ValueError as exc:
            return _malformed_name_response(
                exc,
                suggested_tool="list_entities",
            )
        except EntityNotFoundError as exc:
            return _unknown_name_response(
                exc,
                suggested_tool="list_entities",
                suggested_args=None,
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        return ToolResponse[EntityDetail](
            status="success",
            data=detail,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            follow_up_hints=["describe_table", "describe_column"],
        )

    @app.tool(
        description=(
            "Use this when you have two entity names and need the "
            "canonical SQL join between them. Returns a ready-to-paste "
            "JOIN clause with column mapping. Use `suggest_joins` "
            "instead when you only have physical table names. Don't "
            "use when you need to discover what entities exist — call "
            "`list_entities` first."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("resolve_join")
    def resolve_join(
        entity_a: Annotated[
            str,
            Field(
                description=(
                    "The first entity to join, by name (e.g. "
                    "`customer`). Order doesn't matter — `resolve_join` "
                    "is direction-insensitive; the response preserves "
                    "the direction the join was originally confirmed in."
                ),
            ),
        ],
        entity_b: Annotated[
            str,
            Field(
                description=(
                    "The second entity to join, by name (e.g. `order`). "
                    "Both entities must exist; call `list_entities` if "
                    "unsure."
                ),
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                description=(
                    "When 2+ canonical joins exist between the entity "
                    "pair (billing vs shipping address, primary vs "
                    "secondary user, etc.), pass the canonical-join "
                    "name to disambiguate. Leave null to receive an "
                    "ambiguity-refusal response listing the available "
                    "names."
                ),
            ),
        ] = None,
    ) -> ToolResponse[CanonicalJoinInfo]:
        try:
            info = resolve_join_impl(
                store=store,
                source_connection_id=source_connection_id,
                entity_a=entity_a,
                entity_b=entity_b,
                name=name,
            )
        except ValueError as exc:
            return _malformed_name_response(exc, suggested_tool="list_entities")
        except EntityNotFoundError as exc:
            return _unknown_name_response(exc, suggested_tool="list_entities", suggested_args=None)
        except NoCanonicalJoinError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="no_canonical_join",
                    message=str(exc),
                    recovery=Recovery(suggested_tool="suggest_joins"),
                ),
            )
        except AmbiguousJoinError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="ambiguous_join",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="resolve_join",
                        suggested_args={
                            "entity_a": entity_a,
                            "entity_b": entity_b,
                            "name": exc.candidate_names[0],
                        },
                    ),
                ),
            )
        except JoinNameMismatchError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="join_name_mismatch",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="resolve_join",
                        suggested_args={
                            "entity_a": entity_a,
                            "entity_b": entity_b,
                            "name": exc.canonical_name,
                        },
                    ),
                ),
            )
        except UnknownJoinNameError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="unknown_join_name",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="resolve_join",
                        suggested_args={
                            "entity_a": entity_a,
                            "entity_b": entity_b,
                            "name": exc.candidate_names[0],
                        },
                    ),
                ),
            )
        except Exception as exc:
            return _wrap_internal_error(exc)
        return ToolResponse[CanonicalJoinInfo](
            status="success",
            data=info,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            follow_up_hints=["describe_entity"],
        )

    @app.tool(
        description=(
            "Use this when you have a metric name + want ranked/sliced "
            "rows (top-N, most/highest/lowest). Returns rows + "
            "parameterised SQL. Compiler chains multi-hop joins "
            "automatically (anchor `order_item` + group_by `user.email` "
            "+ order_by `total_items_sold desc` → `order_item → order "
            "→ user`). Pass `order_by=` for deterministic ranking; "
            "without it, `limit` is non-deterministic (envelope flags "
            "`missing_order_by_with_limit`). Use `list_metrics` "
            "instead when you don't know the metric name."
        ),
        annotations=_READ_ONLY_ANNOTATIONS,
    )
    @_trace("get_metric")
    def get_metric(
        name: Annotated[
            str,
            Field(
                description=(
                    "The metric name to compute (e.g. `total_revenue`). "
                    "Call `list_metrics` to enumerate every declared "
                    "metric with its anchor entity, aggregation, and "
                    "time-bucketing capabilities."
                ),
            ),
        ],
        group_by: Annotated[
            tuple[str, ...],
            Field(
                description=(
                    "Tuple of `entity.column` references to slice by "
                    "(e.g. `('product_category.name',)`). Each entity "
                    "must be reachable from the metric's anchor via a "
                    "chain of one or more canonical joins. Multi-hop "
                    "chains (e.g. `order_item → order → user`) resolve "
                    "automatically; if 2+ paths exist, the call refuses "
                    "with `ambiguous_path` and the agent disambiguates "
                    "via the `via` arg. Empty tuple = no slicing."
                ),
            ),
        ] = (),
        filters: Annotated[
            tuple[MetricFilterArg, ...],
            Field(
                description=(
                    "Tuple of `(column, op, value)` predicates. Column is "
                    "`entity.column` form. Ops: eq, ne, lt, lte, gt, gte, "
                    "in, not_in, is_null, not_null. Values bind as "
                    "parameters — never inlined into SQL."
                ),
            ),
        ] = (),
        time_grain: Annotated[
            str | None,
            Field(
                description=(
                    "One of the metric's declared time_grains (day, week, "
                    "month, quarter, year). Defaults null = no time "
                    "bucketing."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=10_000,
                description=(
                    "Max rows returned. Defaults 1000, hard cap 10000. "
                    "The compiler always emits LIMIT regardless of "
                    "group_by complexity."
                ),
            ),
        ] = 1000,
        via: Annotated[
            tuple[str, ...],
            Field(
                description=(
                    "Canonical-join names the chain MUST traverse, used "
                    "to disambiguate `ambiguous_path` (multiple paths "
                    "between anchor and a group_by entity) or "
                    "`ambiguous_join` (parallel canonical joins on a "
                    "single hop). Pass one or more join names returned "
                    "by `list_joins` or by the prior error's "
                    "`candidate_paths` / `candidate_join_names`. Each "
                    "name must appear on a valid chain; otherwise the "
                    "call refuses with `unknown_via_join`. Defaults to "
                    "empty (no constraint) — only set when an ambiguity "
                    "error tells you to."
                ),
            ),
        ] = (),
        order_by: Annotated[
            tuple[MetricOrderByArg, ...],
            Field(
                description=(
                    "ORDER BY clauses applied to the result. Each entry's "
                    "`column` must be EITHER the metric's `name` (the "
                    "measure aggregate's SELECT alias) OR one of the "
                    "`group_by` columns in `entity.column` form. Anything "
                    "else refuses with `unknown_order_by_column`. "
                    "`direction` is `asc` (default) or `desc`. Pass this "
                    'when you want deterministic ranking (e.g. "top 5 '
                    'users by total_items_sold" → '
                    "`order_by=[{column:'total_items_sold', direction:"
                    "'desc'}]`). The compiler auto-appends a tie-breaking "
                    "secondary key (first group_by column ASC) so equal "
                    "measure values produce identical row order across "
                    "runs. Empty tuple = no ORDER BY (database-default "
                    "row order; the envelope surfaces "
                    "`missing_order_by_with_limit` as a degradation "
                    "reason if `limit` is set + `group_by` is non-empty)."
                ),
            ),
        ] = (),
    ) -> ToolResponse[MetricResult]:
        if metric_executor is None:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="internal_error",
                    message=(
                        "get_metric is registered but no metric_executor "
                        "is configured on the server. This is a server-"
                        "config error, not an agent-input error."
                    ),
                    recovery=Recovery(suggested_tool="list_entities"),
                ),
            )
        try:
            result = get_metric_impl(
                store=store,
                executor=metric_executor,
                source_connection_id=source_connection_id,
                name=name,
                group_by=group_by,
                filters=filters,
                time_grain=time_grain,
                limit=limit,
                via=via,
                order_by=order_by,
                pii_block=pii_block,
            )
        except PiiBlockedError as exc:
            return ToolResponse(
                status="refused",
                error=ToolError(
                    kind="pii_blocked",
                    message=str(exc),
                    recovery=Recovery(suggested_tool="describe_entity"),
                    pii_categories=exc.attempted_categories,
                ),
            )
        except UnknownMetricError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="unknown_metric",
                    message=str(exc),
                    recovery=Recovery(suggested_tool="list_entities"),
                ),
            )
        except MalformedColumnError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="malformed_name",
                    message=str(exc),
                    recovery=Recovery(suggested_tool="describe_entity"),
                ),
            )
        except UnknownColumnError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="unknown_name",
                    message=str(exc),
                    recovery=Recovery(suggested_tool="list_entities"),
                ),
            )
        except UnreachableEntityError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="unreachable_entity",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="resolve_join",
                        suggested_args={
                            "entity_a": exc.anchor_entity,
                            "entity_b": exc.target_entity,
                        },
                    ),
                ),
            )
        except CompilerAmbiguousJoinError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="ambiguous_join",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="get_metric",
                        # Hint at calling get_metric again with the via
                        # arg constrained to one of the parallel joins.
                        # Picking candidate_join_names[0] is informational
                        # only — the agent should choose based on the
                        # semantic difference between the parallels
                        # (e.g. billing vs shipping).
                        suggested_args={
                            "via": (exc.candidate_join_names[0],),
                        },
                    ),
                ),
            )
        except CompilerAmbiguousPathError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="ambiguous_path",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="get_metric",
                        # Suggest the first candidate path's full
                        # canonical-join chain as a starting via. The
                        # agent picks among candidate_paths based on
                        # which path semantically matches the user's
                        # question.
                        suggested_args={
                            "via": (exc.candidate_paths[0] if exc.candidate_paths else ()),
                        },
                    ),
                ),
            )
        except UnknownViaJoinError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="unknown_via_join",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="list_joins",
                    ),
                ),
            )
        except UnknownOrderByColumnError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="unknown_order_by_column",
                    message=str(exc),
                    recovery=Recovery(
                        suggested_tool="get_metric",
                        # Hand the agent the exact allowed-column set
                        # from the error payload so the retry is
                        # mechanical — pick any of these as the column
                        # ref. No need to call list_metrics again.
                        suggested_args={"allowed_columns": exc.allowed_columns},
                    ),
                ),
            )
        except InvalidTimeGrainError as exc:
            return ToolResponse(
                status="error",
                error=ToolError(
                    kind="invalid_time_grain",
                    message=str(exc),
                    recovery=Recovery(suggested_tool="get_metric"),
                ),
            )
        except RuntimeError as exc:
            return _wrap_internal_error(exc)
        except Exception as exc:  # pragma: no cover — defensive
            return _wrap_internal_error(exc)
        # Degradation precedence: fan_out_join takes priority over
        # missing_order_by_with_limit because fan-out is a correctness
        # concern (rows may be inflated) while missing-order-by is a
        # determinism concern (rows may be ordered randomly). Both are
        # signals worth surfacing, but if forced to pick one
        # `degradation_reason` we surface the more load-bearing one.
        # The PR-6.5 polish bundle considers structured multi-reason
        # support; for v1 a single enum keeps the contract tight.
        degradation: DegradationReason | None = None
        if result.fan_out_join_names:
            degradation = "fan_out_join"
        elif group_by and not order_by:
            # `limit` is always set (defaults to 1000, hard cap 10_000)
            # — so any non-empty group_by without order_by produces a
            # potentially non-deterministic slice. Surface it even when
            # the caller used the default limit.
            degradation = "missing_order_by_with_limit"
        if degradation is not None:
            return ToolResponse[MetricResult](
                status="degraded",
                data=result,
                confidence="MEDIUM",
                provenance=Provenance(source="schema"),
                follow_up_hints=["describe_entity"],
                degradation_reason=degradation,
            )
        return ToolResponse[MetricResult](
            status="success",
            data=result,
            confidence="HIGH",
            provenance=Provenance(source="schema"),
            follow_up_hints=["describe_entity"],
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
    metric_executor: MetricExecutor | None = None,
    event_bus: EventBus | None = None,
    server_session_id: str | None = None,
    audit_writer: AuditWriter | None = None,
    pii_block: frozenset[PIICategory] = frozenset(),
    tracer: Tracer | None = None,
) -> None:
    """Build the server and run it forever on stdio.

    Blocks until the client disconnects. Used by the `schemabrain serve`
    CLI subcommand.

    `metric_executor` carries the read-only engine `get_metric` runs
    compiled SQL through. Required for `get_metric` to actually execute;
    without it the tool returns `internal_error`. CLI `_cmd_serve`
    wires this from the same URL passed to `index`.

    `event_bus` is the optional observability sink. When set, server
    lifecycle events (server_start / server_stop) plus one event per
    tool call land on the bus. Defaults to `NullEventBus` (no-op).

    `audit_writer` is the optional `mcp_audit` table writer. When set,
    every tool call writes one row to the table. Closed in the finally
    block so a clean shutdown finalises the chain.

    Defensively configures stderr-only logging if no caller has done so
    already — stdout is the JSON-RPC wire here, and a stray log byte
    would corrupt the MCP frame. Skipped entirely when the CLI (or any
    other caller) already attached our named handler, so the caller's
    chosen verbosity is respected.
    """
    import logging as _logging

    from schemabrain.logging_config import _HANDLER_NAME, configure_logging
    from schemabrain.observability import Event, now_iso_utc

    pkg_logger = _logging.getLogger("schemabrain")
    already_configured = any(getattr(h, "name", None) == _HANDLER_NAME for h in pkg_logger.handlers)
    if not already_configured:
        configure_logging()
    bus = event_bus if event_bus is not None else NullEventBus()
    session_id = server_session_id or str(uuid.uuid4())
    app = build_server(
        store=store,
        source_connection_id=source_connection_id,
        embedder=embedder,
        metric_executor=metric_executor,
        event_bus=bus,
        server_session_id=session_id,
        audit_writer=audit_writer,
        pii_block=pii_block,
        tracer=tracer,
    )
    bus.emit(
        Event(
            timestamp=now_iso_utc(),
            server_session_id=session_id,
            kind="server_event",
            event_subtype="server_start",
            message=f"schemabrain serve started (session {session_id})",
        )
    )
    try:
        app.run(transport="stdio")
    finally:
        bus.emit(
            Event(
                timestamp=now_iso_utc(),
                server_session_id=session_id,
                kind="server_event",
                event_subtype="server_stop",
                message=f"schemabrain serve stopped (session {session_id})",
            )
        )
        bus.close()
        if audit_writer is not None:
            audit_writer.close()
