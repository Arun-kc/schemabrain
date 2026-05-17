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


class TestFailureLogging:
    """OSError failures dedupe per (tool, exception). Programming
    bugs (RuntimeError, TypeError, ValueError) log every time so
    a regression doesn't go silent after the first occurrence."""

    def _reset_dedup_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        instrument_mod = sys.modules["schemabrain.observability.instrument"]
        monkeypatch.setattr(instrument_mod, "_emit_failure_logged", set())

    def test_oserror_dedupes_per_tool(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._reset_dedup_set(monkeypatch)

        class _DiskFullBus:
            def emit(self, event: Event) -> None:
                raise OSError("disk full")

            def close(self) -> None:
                pass

        @instrument(
            tool_name="describe_table",
            bus=_DiskFullBus(),
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        def fake_tool() -> _FakeResponse:
            return _FakeResponse(status="success", data=None)

        fake_tool()
        fake_tool()
        fake_tool()
        captured = capsys.readouterr()
        # OSError is the documented expected case — dedupe per tool.
        assert captured.err.count("OSError") == 1
        assert "describe_table" in captured.err

    def test_oserror_dedup_is_per_tool_not_global(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk-full OSError on tool A must NOT silence the same
        OSError on tool B. The previous module-global set keyed on
        exception type name only had this regression."""
        self._reset_dedup_set(monkeypatch)

        class _DiskFullBus:
            def emit(self, event: Event) -> None:
                raise OSError("disk full")

            def close(self) -> None:
                pass

        bus = _DiskFullBus()
        wrap_a = instrument(
            tool_name="tool_a",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )
        wrap_b = instrument(
            tool_name="tool_b",
            bus=bus,
            redactor=EventRedactor(),
            server_session_id=_SESSION,
        )

        wrap_a(lambda: _FakeResponse(status="success", data=None))()
        wrap_b(lambda: _FakeResponse(status="success", data=None))()
        captured = capsys.readouterr()
        assert "tool_a" in captured.err
        assert "tool_b" in captured.err
        # Two distinct (tool, kind) pairs → two log lines.
        assert captured.err.count("OSError") == 2

    def test_programming_bug_logs_every_time(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-OSError (programming bug) is never deduped. A typo or
        shape regression must stay visible — silencing it would hide
        the bug after a single log line for the rest of the process."""
        self._reset_dedup_set(monkeypatch)

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
        # 3 calls → 3 BUG log lines.
        assert captured.err.count("RuntimeError") == 3
        assert captured.err.count("BUG in describe_table") == 3


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
