"""Optional OpenTelemetry span emission for MCP tool calls.

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set in the environment AND the
`schemabrain[otel]` extra is installed, `init_tracer_from_env` returns
a configured `Tracer`. The `@instrument` decorator wraps each MCP tool
call in a span tagged with `gen_ai.*` semantic-convention attributes.

When either condition is unmet, `init_tracer_from_env` returns `None`
and `@instrument` skips span emission entirely. No mandatory dependency
on `opentelemetry-*` packages; `pip install schemabrain` works
identically with or without the extra.

Attributes emitted follow the OTel `gen_ai.*` semantic conventions
(experimental as of 2026). The `set_tool_span_attributes` helper
isolates the attribute names so the next convention rename touches one
place.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any, Final

# Import-guarded — the `[otel]` extra is genuinely optional. When
# `opentelemetry` isn't installed, all OTel paths in this module
# degrade to None / no-op. The `# type: ignore` markers exist because
# the fallback assignments shadow real classes; mypy/pyright would
# otherwise flag the narrowing as unsound.
try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,  # type: ignore[import-not-found]
    )
    from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,  # type: ignore[import-not-found]
    )
    from opentelemetry.trace import (
        Status,  # type: ignore[import-not-found]
        StatusCode,  # type: ignore[import-not-found]
    )

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised via monkeypatch in tests
    OTLPSpanExporter = None  # type: ignore[assignment,misc]
    Resource = None  # type: ignore[assignment,misc]
    TracerProvider = None  # type: ignore[assignment,misc]
    BatchSpanProcessor = None  # type: ignore[assignment,misc]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    _OTEL_AVAILABLE = False


if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer


_ENV_OTLP_ENDPOINT: Final[str] = "OTEL_EXPORTER_OTLP_ENDPOINT"
_SERVICE_NAME: Final[str] = "schemabrain"
SPAN_NAME: Final[str] = "execute_tool"

# Mirror the EventStatus → OTel StatusCode mapping. Charter
# `success`/`empty`/`partial`/`degraded` all read as OTel OK because
# the tool completed and the agent has a usable envelope. `error`
# and `refused` map to ERROR because the agent must take a recovery
# path. The split mirrors `_FAILURE_STATUSES` in `event.py`.
_OK_STATUSES: Final[frozenset[str]] = frozenset({"success", "empty", "partial", "degraded"})
_ERROR_STATUSES: Final[frozenset[str]] = frozenset({"error", "refused"})

# One-shot stderr warning when the env var is set but the extra isn't
# installed. Without this guard, a misconfigured serve quietly produces
# zero traces — the operator thinks OTel is on.
_warned_extra_missing = False


def is_otel_available() -> bool:
    """Return True when the optional `[otel]` extra is importable."""
    return _OTEL_AVAILABLE


def init_tracer_from_env() -> Tracer | None:
    """Initialise an OTel tracer from environment when configured.

    Returns a configured `Tracer` when ALL three conditions hold:
      - the `schemabrain[otel]` extra is installed
      - `OTEL_EXPORTER_OTLP_ENDPOINT` is set in the environment
      - the exporter constructs without raising

    Returns `None` otherwise. Callers MUST treat `None` as "OTel is
    off; skip span emission." This function never raises.

    When `OTEL_EXPORTER_OTLP_ENDPOINT` is set but the extra is missing,
    emit a one-shot stderr warning so a misconfigured operator sees
    the silent disablement.

    The returned tracer uses a private `TracerProvider` — the global
    provider is left alone so an embedding agent harness's tracing is
    not disturbed. `BatchSpanProcessor` registers an atexit hook on
    the provider, so spans flush on interpreter shutdown.
    """
    endpoint = os.environ.get(_ENV_OTLP_ENDPOINT)
    if not endpoint:
        return None
    if not _OTEL_AVAILABLE:
        global _warned_extra_missing
        if not _warned_extra_missing:
            _warned_extra_missing = True
            print(
                f"schemabrain observability: {_ENV_OTLP_ENDPOINT} is set "
                f"but the `schemabrain[otel]` extra is not installed. "
                f"Spans will NOT be exported. Install with "
                f"`pip install schemabrain[otel]` to enable.",
                file=sys.stderr,
            )
        return None
    try:
        resource = Resource.create({"service.name": _SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        # OTLPSpanExporter reads OTEL_EXPORTER_OTLP_ENDPOINT,
        # OTEL_EXPORTER_OTLP_HEADERS, and OTEL_EXPORTER_OTLP_TIMEOUT
        # from the environment automatically.
        exporter = OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        return provider.get_tracer(_SERVICE_NAME)
    except Exception as exc:  # pragma: no cover — defensive
        print(
            f"schemabrain observability: OTel tracer init failed "
            f"({type(exc).__name__}: {exc}). Spans will NOT be "
            f"exported. Continuing without tracing.",
            file=sys.stderr,
        )
        return None


def set_tool_span_attributes(
    span: Span,
    *,
    tool_name: str,
    server_session_id: str,
    status: str,
    duration_ms: float,
    error_kind: str | None,
    fingerprint_hex: str | None,
    result_summary: dict[str, Any] | None,
) -> None:
    """Apply gen_ai.* + schemabrain.* attributes to one tool-call span.

    Isolates the attribute names so the next semantic-conventions
    rename touches one helper. The first guard makes the function safe
    to call unconditionally — when OTel is not installed the call
    is a cheap no-op rather than an AttributeError on Status/StatusCode.
    """
    if not _OTEL_AVAILABLE:
        return
    span.set_attribute("gen_ai.system", _SERVICE_NAME)
    span.set_attribute("gen_ai.tool.name", tool_name)
    if fingerprint_hex is not None:
        span.set_attribute("gen_ai.tool.call.id", fingerprint_hex)
    span.set_attribute("schemabrain.session.id", server_session_id)
    span.set_attribute("schemabrain.duration_ms", duration_ms)
    span.set_attribute("schemabrain.status", status)
    if error_kind is not None:
        span.set_attribute("schemabrain.error_kind", error_kind)
    if result_summary:
        for key, value in result_summary.items():
            # OTel attribute values accept str / bool / int / float
            # and sequences of those. Numeric result counts (matches,
            # columns, paths, queries, entities, rows) pass straight
            # through. Strings (fingerprint hex, entity name, column
            # qname) too. Other types are skipped silently to avoid
            # an exporter error mid-call.
            if isinstance(value, str | bool | int | float):
                span.set_attribute(f"schemabrain.result.{key}", value)
    if status in _OK_STATUSES:
        span.set_status(Status(StatusCode.OK))
    elif status in _ERROR_STATUSES:
        # Carry the charter envelope's error_kind into the OTel status
        # description so dashboards group failures by kind without
        # needing to drill into attributes.
        description = error_kind if error_kind is not None else status
        span.set_status(Status(StatusCode.ERROR, description=description))
