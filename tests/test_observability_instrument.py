"""Tests for the @instrument decorator."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import pytest

from schemabrain.observability.bus import EventBus, NullEventBus
from schemabrain.observability.event import Event
from schemabrain.observability.instrument import instrument, now_iso_utc
from schemabrain.observability.redactor import EventRedactor


class _CapturingBus:
    """Bus that records every emit. Satisfies the EventBus Protocol."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.closed = False

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


_SESSION = "11111111-2222-3333-4444-555555555555"


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


class TestNowIsoUtc:
    def test_shape_is_iso8601_z(self) -> None:
        s = now_iso_utc()
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", s)


class TestDecoratorEmits:
    def test_success_event_emitted(self) -> None:
        bus = _CapturingBus()
        redactor = EventRedactor()

        @instrument(
            tool_name="find_relevant_tables",
            bus=bus,
            redactor=redactor,
            server_session_id=_SESSION,
        )
        def fake_tool(query: str, limit: int = 5) -> _FakeResponse:
            return _FakeResponse(
                status="success",
                data=_FakeMatches(matches=[1, 2, 3]),
            )

        result = fake_tool(query="customer churn", limit=3)
        assert result.status == "success"
        assert len(bus.events) == 1
        ev = bus.events[0]
        assert ev.tool_name == "find_relevant_tables"
        assert ev.status == "success"
        assert ev.error_kind is None
        assert ev.server_session_id == _SESSION
        assert ev.kind == "tool_call"
        assert ev.args_summary == {"query": "customer churn", "limit": 3}
        assert ev.result_summary == {"matches": 3}
        assert ev.duration_ms is not None
        assert ev.duration_ms >= 0

    def test_error_event_carries_error_kind(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="describe_table",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="error", data=None, error=_FakeError(kind="internal_error"))

        fake_tool()
        assert len(bus.events) == 1
        assert bus.events[0].status == "error"
        assert bus.events[0].error_kind == "internal_error"

    def test_refused_event(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="get_metric",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="refused", data=None, error=_FakeError(kind="pii_blocked"))

        fake_tool()
        assert bus.events[0].error_kind == "pii_blocked"
        assert bus.events[0].status == "refused"

    def test_empty_kwargs_emits_empty_args_summary(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="list_entities",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="success", data=[])

        fake_tool()
        assert bus.events[0].args_summary == {}

    def test_duration_measured(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="find_relevant_tables",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def slow_tool() -> _FakeResponse:
            time.sleep(0.01)
            return _FakeResponse(status="success", data=_FakeMatches(matches=[]))

        slow_tool()
        # 0.01s ≈ 10ms minimum; allow generous upper bound on slow CI.
        assert bus.events[0].duration_ms >= 9.0

    def test_redactor_strips_connection_url_from_args(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="describe_table",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool(connection_url: str) -> _FakeResponse:
            return _FakeResponse(status="success", data=None)

        fake_tool(connection_url="postgresql://u:p@h/d")
        assert bus.events[0].args_summary["connection_url"] == "<redacted-connection-url>"

    def test_positional_args_captured(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="describe_table",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool(a: str, b: str) -> _FakeResponse:
            return _FakeResponse(status="success", data=None)

        fake_tool("x", "y")
        # Positional args land under __args by convention.
        assert "__args" in bus.events[0].args_summary
        assert bus.events[0].args_summary["__args"] == ["x", "y"]


class TestDecoratorFailureSafety:
    def test_bus_failure_does_not_propagate(self) -> None:
        class _BrokenBus:
            def emit(self, event: Event) -> None:
                raise RuntimeError("bus exploded")

            def close(self) -> None:
                pass

        @instrument(
            tool_name="describe_table",
            bus=_BrokenBus(),
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="success", data=None)

        # Must not raise.
        result = fake_tool()
        assert result.status == "success"

    def test_extractor_failure_does_not_propagate(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="find_relevant_tables",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            # `find_relevant_tables` extractor expects `.matches`; pass
            # a shape where len(matches) raises.
            class _Hostile:
                @property
                def matches(self) -> int:
                    return 42  # int has no len()

            return _FakeResponse(status="success", data=_Hostile())

        fake_tool()
        assert len(bus.events) == 1
        assert bus.events[0].result_summary == {}

    def test_response_missing_status_attribute_dropped_silently(self) -> None:
        bus = _CapturingBus()

        @instrument(
            tool_name="describe_table",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> object:
            return object()  # no status, no error, no data

        # Must not raise; event will fail to construct (status None for
        # tool_call is invalid) and the failure is swallowed.
        result = fake_tool()
        assert result is not None
        assert len(bus.events) == 0


class TestErrorWithoutKind:
    def test_error_object_present_but_kind_none(self) -> None:
        """A response carrying an `error` whose `kind` is None should
        leave `error_kind` unset rather than crash.
        """
        bus = _CapturingBus()

        class _ErrorMissingKind:
            kind = None

        @instrument(
            tool_name="describe_table",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            r = _FakeResponse(status="error", data=None)
            r.error = _ErrorMissingKind()  # type: ignore[assignment]
            return r

        fake_tool()
        assert len(bus.events) == 1
        assert bus.events[0].error_kind is None


class TestFailureLogOncePerKind:
    def test_repeated_emit_failure_logs_once(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reset the module-level kind tracker between tests to avoid
        # cross-test contamination. Resolve via sys.modules because the
        # package re-exports the `instrument` name, shadowing the
        # submodule for `import as` and attribute-access lookups.
        import sys

        instrument_mod = sys.modules["schemabrain.observability.instrument"]
        monkeypatch.setattr(instrument_mod, "_emit_failure_logged", set())

        class _BrokenBus:
            def emit(self, event: Event) -> None:
                raise RuntimeError("bus exploded")

            def close(self) -> None:
                pass

        @instrument(
            tool_name="describe_table",
            bus=_BrokenBus(),
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="success", data=None)

        fake_tool()
        fake_tool()
        fake_tool()
        captured = capsys.readouterr()
        assert captured.err.count("RuntimeError") == 1


class TestNullBusIntegration:
    def test_null_bus_decorator_no_op(self) -> None:
        @instrument(
            tool_name="describe_table",
            bus=NullEventBus(),
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="success", data=None)

        result = fake_tool()
        assert result.status == "success"


class TestProtocolConformance:
    def test_capturing_bus_satisfies_protocol(self) -> None:
        bus: EventBus = _CapturingBus()
        bus.close()
