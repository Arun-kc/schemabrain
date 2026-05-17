"""`@instrument` — single chokepoint for emitting tool-call events.

Applied as a decorator on each MCP tool closure in `build_server`,
between the FastMCP `@app.tool(...)` registration and the underlying
implementation. The decorator:

  1. Records call start.
  2. Calls the inner function and captures its `ToolResponse`.
  3. Computes `duration_ms`.
  4. Redacts the kwargs via `EventRedactor`.
  5. Runs the per-tool result extractor against `response.data`.
  6. Builds and emits one `Event`.

The decorator NEVER fails the request. Any exception during event
construction or emission is caught, logged once to stderr, and the
underlying `ToolResponse` is returned unchanged.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar

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


def instrument(
    *,
    tool_name: str,
    bus: EventBus,
    redactor: EventRedactor,
    server_session_id: str,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Return a decorator that emits one Event per call.

    The decorated function must return a Charter `ToolResponse`-shaped
    object — anything with `.status`, optional `.error.kind`, and
    optional `.data`. The decorator does NOT validate the response
    shape; if shape is wrong, the emit step catches the AttributeError
    and drops the event rather than blowing up the tool.
    """

    def outer(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> T:
            start = time.perf_counter()
            response = fn(*args, **kwargs)
            duration_ms = (time.perf_counter() - start) * 1000.0
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
