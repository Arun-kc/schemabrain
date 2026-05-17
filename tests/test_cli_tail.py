"""Tests for the `schemabrain tail` CLI subcommand."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from schemabrain.cli import main as cli_main


def _write_event(
    path: Path,
    *,
    timestamp: str = "2026-05-17T12:00:00.000000Z",
    tool_name: str = "find_relevant_tables",
    status: str = "success",
    args: dict | None = None,
    result: dict | None = None,
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
                "error_kind": None,
                "duration_ms": 10.0,
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


class TestTailJsonMode:
    def test_no_follow_prints_history(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_event(events_path, tool_name="t1")
        _write_event(events_path, tool_name="t2")
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--events-path",
                str(events_path),
                "--since",
                "1h",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 2
        names = [json.loads(ln)["tool_name"] for ln in lines]
        assert names == ["t1", "t2"]

    def test_since_filters(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_event(events_path, timestamp="2020-01-01T00:00:00.000000Z", tool_name="old")
        # `--since 1h` filters anything older than 1 hour, so "old"
        # from 2020 is excluded.
        _write_event(events_path, timestamp=_now_iso(), tool_name="recent")
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--events-path",
                str(events_path),
                "--since",
                "1h",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        names = [json.loads(ln)["tool_name"] for ln in lines]
        assert names == ["recent"]


class TestTailPrettyMode:
    def test_no_follow_pretty_render(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_event(
            events_path,
            tool_name="find_relevant_tables",
            args={"query": "customer churn"},
            result={"matches": 3},
        )
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--events-path",
                str(events_path),
                "--since",
                "1h",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "find_relevant_tables" in captured.out
        assert "customer churn" in captured.out
        assert "matches=3" in captured.out

    def test_server_event_renders(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_event(
            events_path,
            kind="server_event",
            event_subtype="server_start",
            message="serving",
        )
        cli_main(
            [
                "tail",
                "--no-follow",
                "--events-path",
                str(events_path),
                "--since",
                "1h",
            ]
        )
        captured = capsys.readouterr()
        assert "server_start" in captured.out


class TestTailFlagResolution:
    def test_env_var_used_when_flag_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_event(events_path, tool_name="from_env")
        monkeypatch.setenv("SCHEMABRAIN_EVENTS_PATH", str(events_path))
        cli_main(["tail", "--no-follow", "--json", "--since", "1h"])
        captured = capsys.readouterr()
        names = [json.loads(ln)["tool_name"] for ln in captured.out.splitlines() if ln.strip()]
        assert names == ["from_env"]

    def test_flag_overrides_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        flag_path = tmp_path / "flag.jsonl"
        env_path = tmp_path / "env.jsonl"
        _write_event(flag_path, tool_name="from_flag")
        _write_event(env_path, tool_name="from_env")
        monkeypatch.setenv("SCHEMABRAIN_EVENTS_PATH", str(env_path))
        cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--events-path",
                str(flag_path),
                "--since",
                "1h",
            ]
        )
        captured = capsys.readouterr()
        names = [json.loads(ln)["tool_name"] for ln in captured.out.splitlines() if ln.strip()]
        assert names == ["from_flag"]


class TestTailErrorHandling:
    def test_bad_since_returns_exit_2(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        events_path = tmp_path / "events.jsonl"
        _write_event(events_path, tool_name="t1")
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--events-path",
                str(events_path),
                "--since",
                "not-a-duration",
            ]
        )
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "error" in captured.err.lower()

    def test_missing_events_file_no_follow_no_output(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        events_path = tmp_path / "nope.jsonl"
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--events-path",
                str(events_path),
                "--since",
                "1h",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == ""


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
