"""Tests for the Event dataclass + JSON serialisation contract."""

from __future__ import annotations

import json

import pytest

from schemabrain.observability.event import Event


def _tool_call_kwargs() -> dict:
    return dict(
        timestamp="2026-05-17T14:32:07.114523Z",
        server_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        kind="tool_call",
        tool_name="find_relevant_tables",
        args_summary={"query": "customer churn"},
        status="success",
        error_kind=None,
        duration_ms=47.3,
        result_summary={"matches": 3},
    )


def _server_event_kwargs() -> dict:
    return dict(
        timestamp="2026-05-17T14:32:00.000000Z",
        server_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        kind="server_event",
        event_subtype="server_start",
        message="schemabrain serve started",
    )


class TestEventFrozen:
    def test_construct_tool_call_event(self) -> None:
        ev = Event(**_tool_call_kwargs())
        assert ev.kind == "tool_call"
        assert ev.tool_name == "find_relevant_tables"
        assert ev.duration_ms == 47.3

    def test_construct_server_event(self) -> None:
        ev = Event(**_server_event_kwargs())
        assert ev.kind == "server_event"
        assert ev.event_subtype == "server_start"
        assert ev.message == "schemabrain serve started"

    def test_event_is_frozen(self) -> None:
        ev = Event(**_tool_call_kwargs())
        with pytest.raises(AttributeError):
            ev.tool_name = "other"  # type: ignore[misc]


class TestEventValidation:
    def test_tool_call_requires_tool_name(self) -> None:
        kw = _tool_call_kwargs()
        kw["tool_name"] = None
        with pytest.raises(ValueError, match="tool_name"):
            Event(**kw)

    def test_tool_call_requires_status(self) -> None:
        kw = _tool_call_kwargs()
        kw["status"] = None
        with pytest.raises(ValueError, match="status"):
            Event(**kw)

    def test_tool_call_requires_duration_ms(self) -> None:
        kw = _tool_call_kwargs()
        kw["duration_ms"] = None
        with pytest.raises(ValueError, match="duration_ms"):
            Event(**kw)

    def test_tool_call_rejects_event_subtype(self) -> None:
        kw = _tool_call_kwargs()
        kw["event_subtype"] = "server_start"
        with pytest.raises(ValueError, match="event_subtype"):
            Event(**kw)

    def test_tool_call_rejects_message(self) -> None:
        kw = _tool_call_kwargs()
        kw["message"] = "hello"
        with pytest.raises(ValueError, match="message"):
            Event(**kw)

    def test_server_event_requires_subtype(self) -> None:
        kw = _server_event_kwargs()
        kw["event_subtype"] = None
        with pytest.raises(ValueError, match="event_subtype"):
            Event(**kw)

    def test_server_event_requires_message(self) -> None:
        kw = _server_event_kwargs()
        kw["message"] = None
        with pytest.raises(ValueError, match="message"):
            Event(**kw)

    def test_server_event_rejects_tool_name(self) -> None:
        kw = _server_event_kwargs()
        kw["tool_name"] = "x"
        with pytest.raises(ValueError, match="tool_name"):
            Event(**kw)

    def test_server_event_rejects_status(self) -> None:
        kw = _server_event_kwargs()
        kw["status"] = "success"
        with pytest.raises(ValueError, match="status"):
            Event(**kw)

    def test_error_kind_requires_failure_status(self) -> None:
        kw = _tool_call_kwargs()
        kw["error_kind"] = "unknown_name"
        # status="success" with error_kind populated is incoherent
        with pytest.raises(ValueError, match="error_kind"):
            Event(**kw)

    def test_error_kind_allowed_with_error_status(self) -> None:
        kw = _tool_call_kwargs()
        kw["status"] = "error"
        kw["error_kind"] = "internal_error"
        ev = Event(**kw)
        assert ev.error_kind == "internal_error"

    def test_error_kind_allowed_with_refused_status(self) -> None:
        kw = _tool_call_kwargs()
        kw["status"] = "refused"
        kw["error_kind"] = "pii_blocked"
        ev = Event(**kw)
        assert ev.error_kind == "pii_blocked"

    def test_failure_status_does_not_require_error_kind(self) -> None:
        # Tools that fail without a structured kind still construct;
        # the runtime is responsible for setting error_kind when known.
        kw = _tool_call_kwargs()
        kw["status"] = "error"
        ev = Event(**kw)
        assert ev.error_kind is None

    def test_unknown_kind_rejected(self) -> None:
        kw = _tool_call_kwargs()
        kw["kind"] = "wat"
        with pytest.raises(ValueError, match="kind"):
            Event(**kw)

    def test_unknown_status_rejected(self) -> None:
        kw = _tool_call_kwargs()
        kw["status"] = "wat"
        with pytest.raises(ValueError, match="status"):
            Event(**kw)

    def test_unknown_server_subtype_rejected(self) -> None:
        kw = _server_event_kwargs()
        kw["event_subtype"] = "wat"
        with pytest.raises(ValueError, match="event_subtype"):
            Event(**kw)

    def test_server_event_rejects_duration_ms(self) -> None:
        kw = _server_event_kwargs()
        kw["duration_ms"] = 1.0
        with pytest.raises(ValueError, match="duration_ms"):
            Event(**kw)

    def test_server_event_rejects_error_kind(self) -> None:
        kw = _server_event_kwargs()
        kw["error_kind"] = "internal_error"
        with pytest.raises(ValueError, match="error_kind"):
            Event(**kw)


class TestEventJsonSerialisation:
    def test_tool_call_round_trip(self) -> None:
        ev = Event(**_tool_call_kwargs())
        line = ev.to_json_line()
        # Round-trip via json then dict-compare to avoid key-ordering
        parsed = json.loads(line)
        assert parsed["tool_name"] == "find_relevant_tables"
        assert parsed["status"] == "success"
        assert parsed["args_summary"] == {"query": "customer churn"}
        # Newline excluded from the JSON body — JSON line ends with \n
        assert line.endswith("\n")
        assert "\n" not in line[:-1]

    def test_server_event_round_trip(self) -> None:
        ev = Event(**_server_event_kwargs())
        parsed = json.loads(ev.to_json_line())
        assert parsed["kind"] == "server_event"
        assert parsed["event_subtype"] == "server_start"
        # None fields included for downstream consumers
        assert parsed["tool_name"] is None

    def test_pydantic_model_in_args_summary_round_trips(self) -> None:
        """F1-C regression: ``order_by`` arrives as a tuple of Pydantic
        ``MetricOrderByArg`` models, which ``dataclasses.asdict`` leaves
        un-converted. ``to_json_line`` MUST flatten them via ``model_dump``
        so the bus doesn't drop the event silently and ``schemabrain tail``
        keeps showing structured-args metric calls.
        """
        from schemabrain.mcp.shapes import MetricOrderByArg

        kw = _tool_call_kwargs()
        kw["tool_name"] = "get_metric"
        kw["args_summary"] = {
            "name": "total_revenue",
            "group_by": ["product.name"],
            "order_by": (MetricOrderByArg(column="total_revenue", direction="desc"),),
        }
        ev = Event(**kw)
        parsed = json.loads(ev.to_json_line())
        assert parsed["args_summary"]["order_by"] == [
            {"column": "total_revenue", "direction": "desc"},
        ]

    def test_frozenset_in_args_summary_round_trips(self) -> None:
        """Adjacent: frozensets are common in the audit / firewall layer
        (``pii_block``, propagated category sets). JSON has no set type,
        so the default hook emits them as sorted lists for deterministic
        wire shape. Without this the bus would drop any event carrying
        a frozenset alongside the Pydantic case.
        """
        kw = _tool_call_kwargs()
        kw["args_summary"] = {"pii_block": frozenset({"credential", "payment_card"})}
        parsed = json.loads(Event(**kw).to_json_line())
        assert parsed["args_summary"]["pii_block"] == ["credential", "payment_card"]

    def test_unhandled_shape_is_loud_not_silent(self) -> None:
        """The default hook raises ``TypeError`` for shapes it doesn't
        understand. The event bus catches that on its drop path; a new
        un-handled shape surfaces as a loud failure here so we add a
        rule rather than discover the gap in production tail-stream
        gaps.
        """

        class _UnknownShape:
            pass

        kw = _tool_call_kwargs()
        kw["args_summary"] = {"weird": _UnknownShape()}
        with pytest.raises(TypeError, match="_UnknownShape"):
            Event(**kw).to_json_line()
