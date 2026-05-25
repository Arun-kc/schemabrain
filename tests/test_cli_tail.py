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
    timestamp: str | None = None,
    tool_name: str = "find_relevant_tables",
    status: str = "success",
    args: dict | None = None,
    result: dict | None = None,
    kind: str = "tool_call",
    event_subtype: str | None = None,
    message: str | None = None,
) -> None:
    # Default to "now" so `--since 1h`-style filters in tests resolve
    # to a window that always includes events written immediately
    # before the CLI runs. A hardcoded literal would only pass on the
    # date it was authored, then silently degrade as time passed.
    if timestamp is None:
        timestamp = _now_iso()
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


class TestTailStorePathResolution:
    """Regression coverage: `tail --store-path` was rejected with an
    unhelpful argparse "unrecognized arguments" error. Operators
    reflexively pass `--store-path` (every other subcommand accepts
    it). Accept it as a documented surface-parity flag and use it
    as a convenience hint when `<store_dir>/events.jsonl` exists.
    """

    def test_store_path_accepted_without_explicit_events_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Sibling events.jsonl: tail should pick it up.
        store_path = tmp_path / "schemabrain.db"
        events_path = tmp_path / "events.jsonl"
        store_path.touch()
        _write_event(events_path, tool_name="from_store_sibling")
        # No env var, no --events-path; --store-path alone must work.
        monkeypatch.delenv("SCHEMABRAIN_EVENTS_PATH", raising=False)
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--store-path",
                str(store_path),
                "--since",
                "1h",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        names = [json.loads(ln)["tool_name"] for ln in captured.out.splitlines() if ln.strip()]
        assert names == ["from_store_sibling"]

    def test_store_path_ignored_when_no_sibling_events_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # No sibling events.jsonl → fall through to the default path
        # AND emit a one-line note so the operator knows which file
        # we ended up reading. The note is the regression case against
        # this regression finding: silently using the
        # default `~/.schemabrain/events.jsonl` after the operator
        # passed `--store-path` is rarely the right outcome.
        store_path = tmp_path / "schemabrain.db"
        store_path.touch()
        monkeypatch.delenv("SCHEMABRAIN_EVENTS_PATH", raising=False)
        # Steer the default to a tmp location so we don't read the
        # operator's real ~/.schemabrain/events.jsonl.
        fake_default = tmp_path / "nonexistent_default.jsonl"
        monkeypatch.setattr("schemabrain.cli._DEFAULT_EVENTS_PATH", str(fake_default))
        exit_code = cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--store-path",
                str(store_path),
                "--since",
                "1h",
            ]
        )
        assert exit_code == 0
        # Note must mention BOTH the missing sibling AND the fallback
        # path so the operator can either pass --events-path or move
        # the file.
        captured = capsys.readouterr()
        assert "no `events.jsonl` found alongside" in captured.err
        assert "--events-path" in captured.err

    def test_no_note_emitted_when_explicit_events_path_passed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # When the operator already chose the events path explicitly,
        # the note is noise — suppress it. Defends against a future
        # refactor that drops the gating condition.
        store_path = tmp_path / "schemabrain.db"
        explicit_events = tmp_path / "explicit.jsonl"
        store_path.touch()
        _write_event(explicit_events, tool_name="t1")
        monkeypatch.delenv("SCHEMABRAIN_EVENTS_PATH", raising=False)
        cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--store-path",
                str(store_path),
                "--events-path",
                str(explicit_events),
                "--since",
                "1h",
            ]
        )
        captured = capsys.readouterr()
        assert "no `events.jsonl` found" not in captured.err

    def test_explicit_events_path_wins_over_store_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Both --store-path AND --events-path passed: explicit wins.
        # Sibling events.jsonl exists but should be ignored.
        store_path = tmp_path / "schemabrain.db"
        sibling_events = tmp_path / "events.jsonl"
        explicit_events = tmp_path / "explicit.jsonl"
        store_path.touch()
        _write_event(sibling_events, tool_name="from_sibling")
        _write_event(explicit_events, tool_name="from_explicit")
        monkeypatch.delenv("SCHEMABRAIN_EVENTS_PATH", raising=False)
        cli_main(
            [
                "tail",
                "--no-follow",
                "--json",
                "--store-path",
                str(store_path),
                "--events-path",
                str(explicit_events),
                "--since",
                "1h",
            ]
        )
        captured = capsys.readouterr()
        names = [json.loads(ln)["tool_name"] for ln in captured.out.splitlines() if ln.strip()]
        assert names == ["from_explicit"]

    def test_resolve_helper_priority_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pure-function test of `_resolve_tail_events_path` so the
        # documented priority order is locked in without a real reader.
        from schemabrain.cli import _DEFAULT_EVENTS_PATH, _resolve_tail_events_path

        explicit = "/tmp/explicit.jsonl"  # nosec B108 — never opened
        env_value = "/tmp/from_env.jsonl"  # nosec B108
        store = tmp_path / "schemabrain.db"
        sibling = tmp_path / "events.jsonl"
        store.touch()
        sibling.touch()

        # 1. Explicit always wins.
        monkeypatch.setenv("SCHEMABRAIN_EVENTS_PATH", env_value)
        assert _resolve_tail_events_path(events_path=explicit, store_path=str(store)) == explicit

        # 2. Env beats store-derived.
        assert _resolve_tail_events_path(events_path=None, store_path=str(store)) == env_value

        # 3. Store-derived when sibling exists.
        monkeypatch.delenv("SCHEMABRAIN_EVENTS_PATH", raising=False)
        assert _resolve_tail_events_path(events_path=None, store_path=str(store)) == str(sibling)

        # 4. Falls back to default when nothing else applies.
        store_no_sibling = tmp_path / "isolated" / "db.sqlite"
        store_no_sibling.parent.mkdir()
        store_no_sibling.touch()
        assert (
            _resolve_tail_events_path(events_path=None, store_path=str(store_no_sibling))
            == _DEFAULT_EVENTS_PATH
        )

        # 5. No flags at all → default.
        assert _resolve_tail_events_path(events_path=None, store_path=None) == _DEFAULT_EVENTS_PATH


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
