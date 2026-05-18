"""`@instrument` — single chokepoint for emitting tool-call events.

Applied as a decorator on each MCP tool closure in `build_server`,
between the FastMCP `@app.tool(...)` registration and the underlying
implementation. The decorator:

  1. Records call start.
  2. Calls the inner function and captures its `ToolResponse`.
  3. Computes `duration_ms`.
  4. (Optional) writes one row to `mcp_audit` via `AuditWriter`,
     then injects the row's fingerprint hex into the response if the
     response carries a `fingerprint` field (today: `MetricResult`).
  5. Redacts the kwargs via `EventRedactor`.
  6. Runs the per-tool result extractor against `response.data`.
  7. Builds and emits one `Event`.

The decorator NEVER fails the request. Any exception during event
construction, audit write, or bus emission is caught, logged once
(OSError) or every time (programming bug) to stderr, and the
underlying `ToolResponse` is returned unchanged. The audit + bus
paths are independent — an audit failure does not block the bus, and
vice versa.
"""

from __future__ import annotations

import contextlib
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar, cast, overload

from schemabrain.audit.writer import AuditRow, AuditWriter, build_audit_row
from schemabrain.observability.bus import EventBus
from schemabrain.observability.event import Event
from schemabrain.observability.extractors import get_result_extractor
from schemabrain.observability.otel import SPAN_NAME, set_tool_span_attributes
from schemabrain.observability.redactor import EventRedactor

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from opentelemetry.trace import Span, Tracer

# Key on (tool_name, exception_class_name) so one tool's failure mode
# doesn't permanently silence the same exception class for the other
# eight tools. The module-level set is safe today: FastMCP dispatches
# sync tools on the event-loop thread with no thread pool, so the set
# is single-thread.
_emit_failure_logged: set[tuple[str, str]] = set()

T = TypeVar("T")


def now_iso_utc() -> str:
    """ISO 8601 UTC timestamp with microsecond precision and trailing Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@overload
def instrument(
    *,
    tool_name: str,
    bus: EventBus,
    redactor: EventRedactor,
    server_session_id: str,
    audit_writer: None = ...,
    source_connection_id: None = ...,
    tracer: Tracer | None = ...,
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


@overload
def instrument(
    *,
    tool_name: str,
    bus: EventBus,
    redactor: EventRedactor,
    server_session_id: str,
    audit_writer: AuditWriter,
    source_connection_id: str,
    tracer: Tracer | None = ...,
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def instrument(
    *,
    tool_name: str,
    bus: EventBus,
    redactor: EventRedactor,
    server_session_id: str,
    audit_writer: AuditWriter | None = None,
    source_connection_id: str | None = None,
    tracer: Tracer | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Return a decorator that emits one Event per call.

    The decorated function must return a Charter `ToolResponse`-shaped
    object — anything with `.status`, optional `.error.kind`, and
    optional `.data`. The decorator does NOT validate the response
    shape; if shape is wrong, the emit step catches the AttributeError
    and drops the event rather than blowing up the tool.

    `audit_writer` and `source_connection_id` are a pair — either both
    set or both unset. The overloads above push this constraint into
    the type system; the runtime check below catches the same misuse
    for callers that bypass type checking.

    `tracer` is the optional OpenTelemetry tracer obtained from
    `init_tracer_from_env()`. When non-None, each tool call is wrapped
    in a span tagged with `gen_ai.*` attributes; the span lifetime
    covers fn() + audit write + fingerprint injection so OTel dashboards
    see schemabrain's full per-call boundary. When None, span emission
    is skipped entirely (no overhead beyond a single Python `if`).
    """
    if audit_writer is not None and source_connection_id is None:
        raise ValueError("instrument(audit_writer=...) requires source_connection_id to be set")

    def outer(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> T:
            start = time.perf_counter()
            # `nullcontext(None)` enters None; the tracer branch enters
            # a real Span. The union annotation keeps the type checker
            # honest — without it, `span` reads as `Any` and the
            # `if span is not None` narrowing below is unverified.
            span_cm: AbstractContextManager[Span] | AbstractContextManager[None] = (
                cast("AbstractContextManager[Span]", tracer.start_as_current_span(SPAN_NAME))
                if tracer is not None
                else contextlib.nullcontext(None)
            )
            with span_cm as span:
                response = fn(*args, **kwargs)
                duration_ms = (time.perf_counter() - start) * 1000.0

                # Audit + fingerprint injection. Isolated from the bus
                # path below so an audit failure does not block tail.
                audit_row: AuditRow | None = None
                if audit_writer is not None:
                    # source_connection_id is guaranteed non-None by the
                    # pair-validation in instrument() above; the assert
                    # narrows for the type checker without runtime cost.
                    assert source_connection_id is not None
                    audit_row = _safe_audit_write(
                        writer=audit_writer,
                        tool_name=tool_name,
                        source_connection_id=source_connection_id,
                        response=response,
                    )
                if audit_row is not None:
                    response = _maybe_inject_fingerprint(response, audit_row.fingerprint_hex)

                if span is not None:
                    _safe_set_span_attrs(
                        span=span,
                        tool_name=tool_name,
                        server_session_id=server_session_id,
                        response=response,
                        duration_ms=duration_ms,
                        fingerprint_hex=(audit_row.fingerprint_hex if audit_row else None),
                    )

            _safe_emit(
                bus=bus,
                redactor=redactor,
                tool_name=tool_name,
                server_session_id=server_session_id,
                args=args,
                kwargs=kwargs,
                response=response,
                duration_ms=duration_ms,
            )
            return response

        return inner

    return outer


def _safe_audit_write(
    *,
    writer: AuditWriter,
    tool_name: str,
    source_connection_id: str,
    response: Any,
) -> AuditRow | None:
    """Write one audit row; swallow + log on failure.

    Returns the persisted `AuditRow` on success, `None` on any
    failure. Callers MUST treat `None` as "no record persisted; do
    not surface the row's fingerprint via the response."
    """
    try:
        draft = build_audit_row(
            tool_name=tool_name,
            source_connection_id=source_connection_id,
            response=response,
        )
        return writer.write(draft)
    except OSError as exc:
        # Disk-full / permission-revoked mid-run — expected at-runtime
        # failures. Dedupe per (tool, exception class).
        _log_failure_once(f"audit:{tool_name}", type(exc).__name__, exc)
        return None
    except Exception as exc:
        # Programming bug — wrong draft shape, mismatched response,
        # writer-side regression. Log every occurrence so a fresh
        # contributor sees the regression immediately.
        print(
            f"schemabrain audit BUG in {tool_name}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


def _maybe_inject_fingerprint(response: Any, fingerprint_hex: str) -> Any:
    """If `response.data` is a Pydantic model with a `fingerprint`
    field, return a new envelope with that field set to `fingerprint_hex`.

    Otherwise return the response unchanged. The duck-typing chain
    (hasattr fingerprint + model_copy) ensures we don't trip on any
    arbitrary MetricResult-shaped class — only on real Pydantic
    models with the field defined.

    When `data` carries a `fingerprint` field but cannot be rewrapped
    (`data` has no `model_copy`, or the outer `response` has no
    `model_copy`), emit a one-shot stderr warning. The audit row was
    still persisted, but the agent sees the placeholder value — a
    silent wire-shape regression otherwise.
    """
    data = getattr(response, "data", None)
    # Use `is None` deliberately: a falsy-but-not-None data object
    # (e.g. an empty list) has no `fingerprint` field, and the next
    # hasattr check returns False. Switching to `if not data:` would
    # also skip legitimate empty containers that DO have the field.
    if data is None:
        return response
    if not hasattr(data, "fingerprint"):
        return response
    if not hasattr(data, "model_copy"):
        _warn_injection_skipped(type(data).__name__, "data.model_copy missing")
        return response
    new_data = data.model_copy(update={"fingerprint": fingerprint_hex})
    if not hasattr(response, "model_copy"):
        _warn_injection_skipped(type(response).__name__, "response.model_copy missing")
        return response
    return response.model_copy(update={"data": new_data})


_injection_skip_logged: set[tuple[str, str]] = set()


def _warn_injection_skipped(type_name: str, reason: str) -> None:
    """One stderr line per (type, reason) pair so a repeating
    misconfiguration doesn't flood logs. Programming bug class —
    visible enough that a fresh contributor sees it the first time."""
    key = (type_name, reason)
    if key in _injection_skip_logged:
        return
    _injection_skip_logged.add(key)
    print(
        f"schemabrain audit: fingerprint injection skipped for "
        f"{type_name} ({reason}). Agent sees the placeholder value; "
        f"audit row was still persisted. This is a wire-shape "
        f"regression — verify the envelope is Pydantic-shaped.",
        file=sys.stderr,
    )


def _extract_response_facets(
    tool_name: str, response: Any
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Pull (status, error_kind, result_summary) off a ToolResponse.

    Shared by `_safe_emit` and `_safe_set_span_attrs` so the bus event
    and the OTel span agree on the same shape extraction, even when
    extractors / response classes drift. The `status` field is narrowed
    to `str | None` even though the source `getattr` returns `Any` —
    every Charter response carries a string-typed status enum, so this
    is a safe narrowing and prevents `Any` leakage into the OTel span
    attribute setter.
    """
    extractor = get_result_extractor(tool_name)
    try:
        result_summary = extractor(getattr(response, "data", None))
    except Exception:  # pragma: no cover — extractors swallow internally
        result_summary = {}
    status_raw = getattr(response, "status", None)
    status: str | None = str(status_raw) if status_raw is not None else None
    error = getattr(response, "error", None)
    error_kind: str | None = None
    if error is not None:
        kind = getattr(error, "kind", None)
        if kind is not None:
            error_kind = str(kind)
    return status, error_kind, result_summary


def _safe_set_span_attrs(
    *,
    span: Span,
    tool_name: str,
    server_session_id: str,
    response: Any,
    duration_ms: float,
    fingerprint_hex: str | None,
) -> None:
    """Apply gen_ai.* + schemabrain.* attributes to one span.

    Mirrors `_safe_emit`'s discipline — OSError is logged once and
    swallowed; programming bugs are logged every time. The OTel SDK
    can raise during attribute / status setting if the span has been
    closed by a concurrent path; we never let that fail the tool call.
    """
    try:
        status, error_kind, result_summary = _extract_response_facets(tool_name, response)
        set_tool_span_attributes(
            span,
            tool_name=tool_name,
            server_session_id=server_session_id,
            status=str(status) if status is not None else "",
            duration_ms=duration_ms,
            error_kind=error_kind,
            fingerprint_hex=fingerprint_hex,
            result_summary=result_summary,
        )
    except OSError as exc:
        _log_failure_once(f"otel:{tool_name}", type(exc).__name__, exc)
    except Exception as exc:  # pragma: no cover — defensive
        print(
            f"schemabrain otel BUG in {tool_name}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _safe_emit(
    *,
    bus: EventBus,
    redactor: EventRedactor,
    tool_name: str,
    server_session_id: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    response: Any,
    duration_ms: float,
) -> None:
    try:
        # FastMCP normally passes keyword arguments. Fall back to a
        # positional capture if a caller passes positionals (rare).
        if args:
            args_summary = redactor.redact({"__args": list(args), **kwargs})
        else:
            args_summary = redactor.redact(kwargs)
        status, error_kind, result_summary = _extract_response_facets(tool_name, response)
        event = Event(
            timestamp=now_iso_utc(),
            server_session_id=server_session_id,
            kind="tool_call",
            tool_name=tool_name,
            args_summary=args_summary,
            status=status,
            error_kind=error_kind,
            duration_ms=duration_ms,
            result_summary=result_summary,
        )
        bus.emit(event)
    except OSError as exc:
        # Expected at-runtime failure (disk full, permission revoked
        # mid-run). Log once per tool and drop the event so the actual
        # tool call still returns to the agent.
        _log_failure_once(tool_name, type(exc).__name__, exc)
    except Exception as exc:
        # Programming bug — wrong Event field, mismatched response
        # shape, redactor crash. Log EVERY occurrence (no dedup) so
        # a fresh contributor sees the regression immediately rather
        # than getting one stderr line followed by silence forever.
        print(
            f"schemabrain instrument BUG in {tool_name}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _log_failure_once(tool_name: str, exc_kind: str, exc: BaseException) -> None:
    """Log one stderr line per (key, exc_kind) and silence repeats.

    Callers prefix `tool_name` with `audit:` / `otel:` / (bare) to
    distinguish which sink dropped. The log message decodes the prefix
    into a human-readable kind so root-cause analysis from logs is
    one-glance rather than "what does `audit:get_metric` mean?"
    """
    key = (tool_name, exc_kind)
    if key in _emit_failure_logged:
        return
    _emit_failure_logged.add(key)
    if tool_name.startswith("audit:"):
        bare = tool_name[len("audit:") :]
        message = f"schemabrain instrument: dropping audit row for {bare} ({exc_kind}: {exc})"
    elif tool_name.startswith("otel:"):
        bare = tool_name[len("otel:") :]
        message = f"schemabrain instrument: dropping OTel span attrs for {bare} ({exc_kind}: {exc})"
    else:
        message = f"schemabrain instrument: dropping bus event for {tool_name} ({exc_kind}: {exc})"
    print(message, file=sys.stderr)
