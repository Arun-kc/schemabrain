"""Tests for the JsonlEventBus — append, rotate, atomicity, failure handling."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from schemabrain.observability.bus import (
    DEFAULT_MAX_BYTES,
    JsonlEventBus,
    NullEventBus,
)
from schemabrain.observability.event import Event


def _tool_event(**overrides: object) -> Event:
    base = dict(
        timestamp="2026-05-17T14:32:07.114523Z",
        server_session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        kind="tool_call",
        tool_name="find_relevant_tables",
        args_summary={"query": "x"},
        status="success",
        error_kind=None,
        duration_ms=10.0,
        result_summary={"matches": 1},
    )
    base.update(overrides)
    return Event(**base)


class TestNullBus:
    def test_emit_noop(self) -> None:
        bus = NullEventBus()
        bus.emit(_tool_event())  # no exception, no side effect
        bus.close()


class TestConstruction:
    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        events_path = tmp_path / "nested" / "events.jsonl"
        JsonlEventBus(events_path)
        assert events_path.parent.is_dir()

    def test_parent_dir_mode_0700(self, tmp_path: Path) -> None:
        events_path = tmp_path / "sb" / "events.jsonl"
        JsonlEventBus(events_path)
        mode = stat.S_IMODE(events_path.parent.stat().st_mode)
        assert mode == 0o700

    def test_path_property_expanded(self) -> None:
        bus = JsonlEventBus("~/.schemabrain_test_unused.jsonl")
        assert "~" not in str(bus.path)

    def test_default_max_bytes_10mib(self, tmp_path: Path) -> None:
        bus = JsonlEventBus(tmp_path / "e.jsonl")
        assert bus.max_bytes == DEFAULT_MAX_BYTES == 10 * 1024 * 1024

    def test_rejects_zero_max_bytes(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            JsonlEventBus(tmp_path / "e.jsonl", max_bytes=0)

    def test_rejects_negative_max_bytes(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes"):
            JsonlEventBus(tmp_path / "e.jsonl", max_bytes=-1)


class TestEmitAppends:
    def test_first_emit_creates_file(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path)
        bus.emit(_tool_event())
        assert events_path.is_file()

    def test_emitted_line_is_valid_json(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path)
        bus.emit(_tool_event(tool_name="describe_table"))
        line = events_path.read_text().strip()
        parsed = json.loads(line)
        assert parsed["tool_name"] == "describe_table"

    def test_two_emits_two_lines(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path)
        bus.emit(_tool_event(tool_name="t1"))
        bus.emit(_tool_event(tool_name="t2"))
        lines = events_path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["tool_name"] == "t1"
        assert json.loads(lines[1])["tool_name"] == "t2"

    def test_file_mode_0600_on_creation(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path)
        bus.emit(_tool_event())
        mode = stat.S_IMODE(events_path.stat().st_mode)
        # umask may strip group/other bits further; require no group/other access
        assert mode & 0o077 == 0


class TestRotation:
    def test_rotation_renames_to_dot_one(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path, max_bytes=200)
        # Emit enough to push past 200 bytes.
        for i in range(20):
            bus.emit(_tool_event(tool_name=f"tool_{i}"))
        assert events_path.is_file()
        assert (tmp_path / "e.jsonl.1").is_file()

    def test_rotation_preserves_previous_dot_one(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path, max_bytes=200)
        for i in range(30):
            bus.emit(_tool_event(tool_name=f"early_{i}"))
        first_rotation = (tmp_path / "e.jsonl.1").read_bytes()
        for i in range(30):
            bus.emit(_tool_event(tool_name=f"late_{i}"))
        second_rotation = (tmp_path / "e.jsonl.1").read_bytes()
        # The .1 file is the most-recent active file, not the first.
        assert first_rotation != second_rotation

    def test_no_rotation_when_below_threshold(self, tmp_path: Path) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path, max_bytes=DEFAULT_MAX_BYTES)
        for _ in range(5):
            bus.emit(_tool_event())
        assert not (tmp_path / "e.jsonl.1").exists()

    def test_rotation_check_skipped_when_file_missing(self, tmp_path: Path) -> None:
        # Forces the FileNotFoundError branch in _rotate_if_needed.
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path, max_bytes=200)
        # Path doesn't exist yet — emit should still succeed.
        bus.emit(_tool_event())
        assert events_path.is_file()


class TestFailureHandling:
    def test_emit_swallows_oserror(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path)

        def boom(*_args: object, **_kw: object) -> int:
            raise OSError("disk full")

        monkeypatch.setattr("schemabrain.observability.bus.os.open", boom)
        # Must not raise.
        bus.emit(_tool_event())

    def test_emit_logs_failure_once_per_kind(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        events_path = tmp_path / "e.jsonl"
        bus = JsonlEventBus(events_path)

        def boom(*_args: object, **_kw: object) -> int:
            raise OSError("disk full")

        monkeypatch.setattr("schemabrain.observability.bus.os.open", boom)
        bus.emit(_tool_event())
        bus.emit(_tool_event())
        bus.emit(_tool_event())
        captured = capsys.readouterr()
        # The first call writes to stderr; subsequent calls of the same
        # exception kind should not.
        assert captured.err.count("OSError") == 1


class TestClose:
    def test_close_idempotent(self, tmp_path: Path) -> None:
        bus = JsonlEventBus(tmp_path / "e.jsonl")
        bus.close()
        bus.close()  # second call should not raise
