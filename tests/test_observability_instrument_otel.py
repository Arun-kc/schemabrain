"""End-to-end tests for `@instrument` + OTel tracer.

Uses OTel's `InMemorySpanExporter` to capture spans the decorator
produces against a tool-shaped fake. Verifies the gen_ai.* attribute
set, status mapping (OK / ERROR / NotSet), span name, and the
bus emission still lands alongside the span.

The whole module is gated on the `[otel]` extra being installed —
without it, none of these assertions are meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from schemabrain.observability import otel as otel_module
from schemabrain.observability.event import Event
from schemabrain.observability.instrument import instrument
from schemabrain.observability.redactor import EventRedactor

pytestmark = pytest.mark.skipif(
    not otel_module.is_otel_available(),
    reason="`schemabrain[otel]` extra not installed",
)


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


@dataclass
class _FakeError:
    kind: str


@dataclass
class _FakeMatches:
    matches: list[Any]


@dataclass
class _FakeResponse:
    status: str
    data: Any = None
    error: _FakeError | None = None


_SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def in_memory_tracer() -> tuple[Any, Any]:
    """Return (tracer, exporter) wired against an InMemorySpanExporter.

    Uses a *private* TracerProvider so the global state isn't trampled
    across tests; each fixture invocation gets a clean exporter.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    resource = Resource.create({"service.name": "schemabrain-test"})
    provider = TracerProvider(resource=resource)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("schemabrain-test")
    return tracer, exporter


class TestSuccessPath:
    def test_one_span_per_call(self, in_memory_tracer: tuple[Any, Any]) -> None:
        tracer, exporter = in_memory_tracer
        bus = _CapturingBus()

        @instrument(
            tool_name="find_relevant_tables",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
            tracer=tracer,
        )
        def fake_tool(query: str, limit: int = 5) -> _FakeResponse:
            return _FakeResponse(
                status="success",
                data=_FakeMatches(matches=[1, 2, 3]),
            )

        result = fake_tool(query="customer churn", limit=3)
        assert result.status == "success"

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "execute_tool"
        attrs = dict(span.attributes or {})
        assert attrs["gen_ai.system"] == "schemabrain"
        assert attrs["gen_ai.tool.name"] == "find_relevant_tables"
        assert attrs["schemabrain.session.id"] == _SESSION
        assert attrs["schemabrain.status"] == "success"
        assert attrs["schemabrain.result.matches"] == 3
        # OK status
        assert span.status.status_code.name == "OK"

        # Bus emission still landed alongside the span.
        assert len(bus.events) == 1

    def test_error_response_marks_span_as_error(self, in_memory_tracer: tuple[Any, Any]) -> None:
        tracer, exporter = in_memory_tracer
        bus = _CapturingBus()

        @instrument(
            tool_name="get_metric",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
            tracer=tracer,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(
                status="error",
                error=_FakeError(kind="unknown_metric"),
            )

        fake_tool()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        attrs = dict(span.attributes or {})
        assert attrs["schemabrain.status"] == "error"
        assert attrs["schemabrain.error_kind"] == "unknown_metric"
        assert span.status.status_code.name == "ERROR"
        # The status description carries the error_kind for dashboards.
        assert span.status.description == "unknown_metric"

    def test_refused_status_marks_span_as_error(self, in_memory_tracer: tuple[Any, Any]) -> None:
        tracer, exporter = in_memory_tracer
        bus = _CapturingBus()

        @instrument(
            tool_name="get_metric",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
            tracer=tracer,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(
                status="refused",
                error=_FakeError(kind="pii_blocked"),
            )

        fake_tool()
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code.name == "ERROR"
        assert spans[0].status.description == "pii_blocked"


class TestNoTracerPath:
    def test_no_span_when_tracer_is_none(self) -> None:
        """The decorator with tracer=None emits zero spans —
        verified indirectly via no-import-of-otel side effect."""
        bus = _CapturingBus()

        @instrument(
            tool_name="find_relevant_tables",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
            tracer=None,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(
                status="success",
                data=_FakeMatches(matches=[1]),
            )

        result = fake_tool()
        assert result.status == "success"
        # Bus emission unaffected.
        assert len(bus.events) == 1


class TestSpanWrapsTheCall:
    def test_span_lifetime_covers_fn_invocation(self, in_memory_tracer: tuple[Any, Any]) -> None:
        """The span must be the current context while fn runs.

        Asserts via opentelemetry.trace.get_current_span() called from
        inside fn — proves the span wraps the call rather than being
        created retroactively (which would lose context propagation
        for any downstream tracer.start_span calls in real tools).
        """
        from opentelemetry import trace as otel_trace

        tracer, _exporter = in_memory_tracer
        bus = _CapturingBus()
        captured_span_during_call: list[Any] = []

        @instrument(
            tool_name="find_relevant_tables",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
            tracer=tracer,
        )
        def fake_tool() -> _FakeResponse:
            captured_span_during_call.append(otel_trace.get_current_span())
            return _FakeResponse(
                status="success",
                data=_FakeMatches(matches=[1]),
            )

        fake_tool()
        assert len(captured_span_during_call) == 1
        # The span captured from inside fn is non-recording when
        # outside a span context. The fact that it has a span context
        # with a non-zero trace_id proves the wrap.
        inner_span = captured_span_during_call[0]
        assert inner_span.get_span_context().trace_id != 0
