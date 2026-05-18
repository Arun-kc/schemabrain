"""Tests for the tail reader + parse_since + pretty-printer."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from schemabrain.observability.tail import (
    TailOptions,
    TailReader,
    parse_event_timestamp,
    parse_since,
    render_event_pretty,
)


class TestParseSinceDurations:
    def test_seconds(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        assert parse_since("30s", now=now) == now - timedelta(seconds=30)

    def test_minutes(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        assert parse_since("5m", now=now) == now - timedelta(minutes=5)

    def test_hours(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        assert parse_since("2h", now=now) == now - timedelta(hours=2)

    def test_days(self) -> None:
        now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
        assert parse_since("1d", now=now) == now - timedelta(days=1)


class TestParseSinceIso8601:
    def test_iso_with_z(self) -> None:
        result = parse_since("2026-05-17T10:00:00Z")
        assert result == datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC)

    def test_iso_with_offset(self) -> None:
        result = parse_since("2026-05-17T15:30:00+05:30")
        assert result == datetime(2026, 5, 17, 10, 0, 0, tzinfo=UTC)

    def test_iso_with_microseconds(self) -> None:
        result = parse_since("2026-05-17T10:00:00.123456Z")
        assert result.microsecond == 123456

    def test_iso_without_tz_rejected(self) -> None:
        with pytest.raises(ValueError, match="must include a timezone"):
            parse_since("2026-05-17T10:00:00")

    def test_unparseable_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="expected duration"):
            parse_since("not-a-date")


class TestParseEventTimestamp:
    def test_round_trip(self) -> None:
        # Match the wire format from Event.to_json_line.
        dt = parse_event_timestamp("2026-05-17T14:32:07.114523Z")
        assert dt.year == 2026
        assert dt.hour == 14
        assert dt.tzinfo == UTC


def _write_event(
    path: Path,
    *,
    timestamp: str = "2026-05-17T12:00:00.000000Z",
    tool_name: str = "find_relevant_tables",
    status: str = "success",
    args: dict | None = None,
    result: dict | None = None,
    error_kind: str | None = None,
    duration_ms: float = 10.0,
    kind: str = "tool_call",
    event_subtype: str | None = None,
    message: str | None = None,
) -> None:
    event: dict[str, object] = {
        "timestamp": timestamp,
        "server_session_id": "test-session",
        "kind": kind,
    }
    if kind == "tool_call":
        event.update(
            {
                "tool_name": tool_name,
                "args_summary": args or {},
                "status": status,
                "error_kind": error_kind,
                "duration_ms": duration_ms,
                "result_summary": result or {},
                "event_subtype": None,
                "message": None,
            }
        )
    else:
        event.update(
            {
                "tool_name": None,
                "args_summary": None,
                "status": None,
                "error_kind": None,
                "duration_ms": None,
                "result_summary": None,
                "event_subtype": event_subtype,
                "message": message,
            }
        )
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


class TestTailReaderHistoryOnly:
    def test_reads_existing_events_then_returns_when_not_follow(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="t1")
        _write_event(events_path, tool_name="t2")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert [e["tool_name"] for e in events] == ["t1", "t2"]

    def test_since_filters_old_events(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, timestamp="2026-05-17T10:00:00.000000Z", tool_name="old")
        _write_event(events_path, timestamp="2026-05-17T12:00:00.000000Z", tool_name="new")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2026, 5, 17, 11, 0, 0, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert [e["tool_name"] for e in events] == ["new"]

    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="t1")
        # Append a malformed line.
        with events_path.open("a") as f:
            f.write("not json\n")
        _write_event(events_path, tool_name="t2")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert [e["tool_name"] for e in events] == ["t1", "t2"]

    def test_no_file_no_follow_yields_nothing(self, tmp_path: Path) -> None:
        events_path = tmp_path / "missing.jsonl"
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert events == []

    def test_empty_lines_skipped(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        events_path.write_text("\n\n\n", encoding="utf-8")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert events == []


class TestTailReaderFollow:
    def test_picks_up_new_events_after_initial_read(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="t1")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=True,
            json_mode=True,
            poll_interval_s=0.02,
        )
        results: list[dict] = []

        def consume() -> None:
            with TailReader(opts) as reader:
                for ev in reader.stream():
                    results.append(ev)
                    if len(results) >= 2:
                        return

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        # Give the reader a chance to read t1.
        time.sleep(0.1)
        _write_event(events_path, tool_name="t2")
        thread.join(timeout=2.0)
        assert [e["tool_name"] for e in results] == ["t1", "t2"]

    def test_follow_waits_for_file_creation(self, tmp_path: Path) -> None:
        events_path = tmp_path / "later.jsonl"
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=True,
            json_mode=True,
            poll_interval_s=0.02,
        )
        results: list[dict] = []

        def consume() -> None:
            with TailReader(opts) as reader:
                for ev in reader.stream():
                    results.append(ev)
                    return

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        time.sleep(0.1)
        _write_event(events_path, tool_name="late_event")
        thread.join(timeout=2.0)
        assert results[0]["tool_name"] == "late_event"

    def test_rotation_detected_via_inode_change(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="pre_rotation")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=True,
            json_mode=True,
            poll_interval_s=0.02,
        )
        results: list[dict] = []

        def consume() -> None:
            with TailReader(opts) as reader:
                for ev in reader.stream():
                    results.append(ev)
                    if len(results) >= 2:
                        return

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        time.sleep(0.1)
        # Simulate rotation: move the existing file aside and write a
        # brand-new file at the same path. Inode changes.
        os.replace(events_path, tmp_path / "e.jsonl.1")
        _write_event(events_path, tool_name="post_rotation")
        thread.join(timeout=2.0)
        names = [e["tool_name"] for e in results]
        assert "pre_rotation" in names
        assert "post_rotation" in names

    def test_partial_line_held_until_complete(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        # Write a partial line (no trailing newline).
        events_path.write_text('{"timestamp":"', encoding="utf-8")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert events == []


class TestRenderEventPretty:
    def _render(self, event: dict) -> str:
        buf = io.StringIO()
        console = Console(file=buf, width=120, force_terminal=False)
        render_event_pretty(event, console)
        return buf.getvalue()

    def test_renders_tool_call_success(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "find_relevant_tables",
            "args_summary": {"query": "customer churn"},
            "status": "success",
            "error_kind": None,
            "duration_ms": 47.3,
            "result_summary": {"matches": 3},
        }
        output = self._render(event)
        assert "find_relevant_tables" in output
        assert "customer churn" in output
        assert "matches=3" in output
        assert "47ms" in output

    def test_renders_error_with_kind(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "describe_table",
            "args_summary": {"qualified_name": "public.nope"},
            "status": "error",
            "error_kind": "unknown_name",
            "duration_ms": 2.1,
            "result_summary": {},
        }
        output = self._render(event)
        assert "ERROR" in output
        assert "unknown_name" in output

    def test_renders_refused(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "get_metric",
            "args_summary": {},
            "status": "refused",
            "error_kind": "pii_blocked",
            "duration_ms": 1.0,
            "result_summary": {},
        }
        output = self._render(event)
        assert "REFUSED" in output
        assert "pii_blocked" in output

    def test_renders_degraded(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "get_metric",
            "args_summary": {},
            "status": "degraded",
            "error_kind": None,
            "duration_ms": 100.0,
            "result_summary": {"rows": 5},
        }
        output = self._render(event)
        assert "degraded" in output
        assert "rows=5" in output

    def test_long_tool_name_truncated_with_ellipsis(self) -> None:
        # Tool names longer than the fixed-width tool column truncate
        # with a single "…" so the args column still starts at a
        # consistent visual position. Defensive against any future MCP
        # tool with a long name (today's longest is 22 chars exactly).
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "find_relevant_tables_and_more_columns",
            "args_summary": {"q": "x"},
            "status": "success",
            "error_kind": None,
            "duration_ms": 1.0,
            "result_summary": {},
        }
        output = self._render(event)
        # Truncated form ends with ellipsis; full name does NOT appear.
        assert "…" in output
        assert "find_relevant_tables_and_more_columns" not in output

    def test_renders_server_event(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "server_event",
            "event_subtype": "server_start",
            "message": "schemabrain serve started",
        }
        output = self._render(event)
        assert "server_start" in output
        assert "schemabrain serve started" in output

    def test_renders_no_duration_when_missing(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "x",
            "args_summary": {},
            "status": "success",
            "error_kind": None,
            "duration_ms": None,
            "result_summary": {},
        }
        output = self._render(event)
        # Should not crash, should not contain "ms"
        assert "x" in output
        assert "ms" not in output

    def test_long_string_arg_truncated_in_display(self) -> None:
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "tool_call",
            "tool_name": "x",
            "args_summary": {"q": "a" * 200},
            "status": "success",
            "error_kind": None,
            "duration_ms": 1.0,
            "result_summary": {},
        }
        output = self._render(event)
        # Display truncates to 60 chars + ellipsis
        assert "…" in output

    def test_bad_timestamp_does_not_crash(self) -> None:
        event = {
            "timestamp": "not-a-real-timestamp",
            "kind": "tool_call",
            "tool_name": "x",
            "args_summary": {},
            "status": "success",
            "error_kind": None,
            "duration_ms": 1.0,
            "result_summary": {},
        }
        output = self._render(event)
        assert "x" in output


class TestPartialLineAcrossMultiplePolls:
    """Regression test for an offset-tracking bug that caused partial
    lines held in the buffer to be re-read from disk and prepended
    to themselves, producing corrupted concatenated lines.
    """

    def test_two_poll_cycles_with_partial_line_in_between(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="early")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=True,
            json_mode=True,
            poll_interval_s=0.02,
        )
        results: list[dict] = []

        def consume() -> None:
            with TailReader(opts) as reader:
                for ev in reader.stream():
                    results.append(ev)
                    if len(results) >= 2:
                        return

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        time.sleep(0.05)
        # Append a partial line (no newline) — the reader should hold
        # it in its buffer without yielding it.
        partial_event = json.dumps(
            {
                "timestamp": "2026-05-17T12:00:00.000000Z",
                "server_session_id": "test-session",
                "kind": "tool_call",
                "tool_name": "later",
                "args_summary": {},
                "status": "success",
                "error_kind": None,
                "duration_ms": 1.0,
                "result_summary": {},
                "event_subtype": None,
                "message": None,
            }
        )
        with events_path.open("a", encoding="utf-8") as f:
            f.write(partial_event)  # no trailing \n
            f.flush()
        time.sleep(0.1)
        # Now complete the line. The reader must produce ONE valid
        # event named "later", not a corrupted concatenation.
        with events_path.open("a", encoding="utf-8") as f:
            f.write("\n")
            f.flush()
        thread.join(timeout=2.0)
        assert [e["tool_name"] for e in results] == ["early", "later"]


class TestTailReaderFileVanishWarning:
    def test_file_disappearance_warns_and_recovers(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="before")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=True,
            json_mode=True,
            poll_interval_s=0.02,
        )
        results: list[dict] = []

        def consume() -> None:
            with TailReader(opts) as reader:
                for ev in reader.stream():
                    results.append(ev)
                    if len(results) >= 2:
                        return

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        time.sleep(0.1)
        # Remove the file entirely. The reader should warn once and
        # keep polling for the file to reappear.
        events_path.unlink()
        time.sleep(0.1)
        # Re-create with a new event.
        _write_event(events_path, tool_name="after")
        thread.join(timeout=2.0)
        names = [e["tool_name"] for e in results]
        assert "before" in names
        assert "after" in names
        captured = capsys.readouterr()
        assert "disappeared" in captured.err


class TestTailReaderExitCleanupUnderException:
    def test_fd_closed_when_exception_in_with_block(self, tmp_path: Path) -> None:
        """TailReader.__exit__ must close the open fd even when the
        caller raises inside the with block."""
        events_path = tmp_path / "e.jsonl"
        _write_event(events_path, tool_name="t1")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2020, 1, 1, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        reader = TailReader(opts)
        try:
            with reader:
                # Advance the stream so fd is assigned.
                _ = next(reader.stream())
                # Caller raises with the fd open.
                raise RuntimeError("caller boom")
        except RuntimeError:
            pass
        assert reader._fd is None


class TestTailReaderTimestampDefensives:
    def test_event_with_missing_timestamp_passes_filter(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        # Hand-write an event with no timestamp — the filter must let it
        # through rather than crashing.
        with events_path.open("w") as f:
            f.write(json.dumps({"kind": "tool_call", "tool_name": "x"}) + "\n")
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2026, 5, 17, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert events[0]["tool_name"] == "x"

    def test_event_with_garbage_timestamp_passes_filter(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        with events_path.open("w") as f:
            f.write(
                json.dumps({"timestamp": "not-iso", "kind": "tool_call", "tool_name": "y"}) + "\n"
            )
        opts = TailOptions(
            events_path=events_path,
            since=datetime(2026, 5, 17, tzinfo=UTC),
            follow=False,
            json_mode=True,
        )
        with TailReader(opts) as reader:
            events = list(reader.stream())
        assert events[0]["tool_name"] == "y"


class TestRenderServerEventNoMessage:
    def test_renders_with_empty_message(self) -> None:
        buf = io.StringIO()
        console = Console(file=buf, width=120, force_terminal=False)
        event = {
            "timestamp": "2026-05-17T14:32:07.114000Z",
            "kind": "server_event",
            "event_subtype": None,  # forces fallback
            "message": None,
        }
        render_event_pretty(event, console)
        # Should not crash.
        out = buf.getvalue()
        assert "server_event" in out
