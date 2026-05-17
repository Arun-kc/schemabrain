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

import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar, overload

from schemabrain.audit.writer import AuditRow, AuditWriter, build_audit_row
from schemabrain.observability.bus import EventBus
from schemabrain.observability.event import Event
from schemabrain.observability.extractors import get_result_extractor
from schemabrain.observability.redactor import EventRedactor

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
) -> Callable[[Callable[..., T]], Callable[..., T]]: ...


def instrument(
    *,
    tool_name: str,
    bus: EventBus,
    redactor: EventRedactor,
    server_session_id: str,
    audit_writer: AuditWriter | None = None,
    source_connection_id: str | None = None,
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
    """
    if audit_writer is not None and source_connection_id is None:
        raise ValueError("instrument(audit_writer=...) requires source_connection_id to be set")

    def outer(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> T:
            start = time.perf_counter()
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
        extractor = get_result_extractor(tool_name)
        try:
            result_summary = extractor(getattr(response, "data", None))
        except Exception:  # pragma: no cover — extractors swallow internally
            result_summary = {}
        status = getattr(response, "status", None)
        error = getattr(response, "error", None)
        error_kind: str | None = None
        if error is not None:
            kind = getattr(error, "kind", None)
            if kind is not None:
                error_kind = str(kind)
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
    key = (tool_name, exc_kind)
    if key in _emit_failure_logged:
        return
    _emit_failure_logged.add(key)
    print(
        f"schemabrain instrument: dropping event for {tool_name} ({exc_kind}: {exc})",
        file=sys.stderr,
    )
